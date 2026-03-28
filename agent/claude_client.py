"""AI client — talks to OpenAI-compatible proxy via the Responses API."""

import json
import re
import urllib.error
import urllib.request
from typing import Callable, Optional

from config import MAX_OUTPUT_TOKENS, SYSTEM_PROMPT as _CONFIG_PROMPT

# Fallback prompt if system_prompt.txt doesn't exist and config has no prompt
_DEFAULT_PROMPT = """\
You are an AI agent controlling a remote Windows PC through physical HID input.
The screen is {w}x{h} pixels. You see a fresh screenshot each turn.

Available actions (reply with ONE raw JSON object):

  {{"type":"click_at","x":<int>,"y":<int>}}
  {{"type":"double_click_at","x":<int>,"y":<int>}}
  {{"type":"right_click_at","x":<int>,"y":<int>}}
  {{"type":"scroll_at","x":<int>,"y":<int>,"dy":<int>}}
  {{"type":"key","keys":["ctrl","c"]}}
  {{"type":"type","text":"hello world"}}
  {{"type":"screenshot"}}
  {{"type":"wait","duration":<seconds>}}
  {{"type":"done","message":"<reason>"}}

One action per turn. Specify pixel coordinates of the CENTER of the target element.
"""


class ParseError(Exception):
    pass


class ClaudeClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        screen_w: int = 1280,
        screen_h: int = 720,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._screen_w = screen_w
        self._screen_h = screen_h
        prompt = _CONFIG_PROMPT or _DEFAULT_PROMPT
        self._base_system = prompt.format(w=screen_w, h=screen_h)
        self._system = self._base_system

        # cost tracking
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0

    # -- public API ----------------------------------------------------------

    def decide(
        self,
        task: str,
        screenshot_b64: str,
        history: list[dict],
        on_token: Optional[Callable[[str], None]] = None,
    ) -> dict:
        input_msgs = self._build_input(task, screenshot_b64, history)

        payload = json.dumps({
            "model": self._model,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "instructions": self._system,
            "input": input_msgs,
            "stream": True,
        })

        req = urllib.request.Request(
            f"{self._base_url}/responses",
            data=payload.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            resp = urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            raise ParseError(f"API {e.code}: {error_body[:500]}") from e

        full_text = ""
        usage_data = {}

        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data: "):
                continue
            chunk = line[6:]
            if chunk == "[DONE]":
                break
            try:
                d = json.loads(chunk)
            except json.JSONDecodeError:
                continue

            etype = d.get("type", "")

            if etype == "response.output_text.delta":
                delta = d.get("delta", "")
                full_text += delta
                if on_token:
                    on_token(delta)

            elif etype == "response.completed":
                r = d.get("response", {})
                usage_data = r.get("usage", {})

        # update cost (gpt-5.4 pricing estimate)
        self.total_input_tokens += usage_data.get("input_tokens", 0)
        self.total_output_tokens += usage_data.get("output_tokens", 0)
        self._print_cost()

        if not full_text.strip():
            raise ParseError("Empty response from AI")

        action = self._parse_response(full_text)
        # Store reasoning text on the action so the caller can log/save it
        action["_reasoning"] = full_text.strip()
        return action

    def get_cost(self) -> dict[str, float]:
        input_cost = (self.total_input_tokens / 1_000_000) * 2.50
        output_cost = (self.total_output_tokens / 1_000_000) * 10.00
        return {
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "input_cost": input_cost,
            "output_cost": output_cost,
            "total_cost": input_cost + output_cost,
        }

    def set_profile(self, profile_text: str) -> None:
        """Append a task profile to the base system prompt."""
        if profile_text:
            self._system = self._base_system + "\n\n" + profile_text
        else:
            self._system = self._base_system

    def reset(self) -> None:
        """No-op for compatibility."""
        pass

    # -- internals -----------------------------------------------------------

    def _print_cost(self) -> None:
        c = self.get_cost()
        print(
            f"[Cost] in={c['input_tokens']} out={c['output_tokens']} "
            f"session=${c['total_cost']:.4f}"
        )

    @staticmethod
    def _build_input(
        task: str, screenshot_b64: str, history: list[dict],
    ) -> list[dict]:
        messages: list[dict] = []

        # rolling history (text only, no images)
        for entry in history[-10:]:
            messages.append({
                "role": "user",
                "content": [{"type": "input_text", "text": entry["user"]}],
            })
            messages.append({
                "role": "assistant",
                "content": [{"type": "output_text", "text": entry["assistant"]}],
            })

        # stuck detection — looks at click targets in a wider window,
        # ignoring keyboard actions in between
        stuck_warning = ""
        if len(history) >= 3:
            def extract_click_bucket(text: str) -> tuple | None:
                """Return (x_bucket, y_bucket) for click actions, None for non-clicks."""
                m = re.search(r'\{[^{}]*\}', text)
                if not m:
                    return None
                try:
                    obj = json.loads(m.group(0))
                except json.JSONDecodeError:
                    return None
                atype = obj.get("type", "")
                if "click" not in atype:
                    return None
                x = obj.get("x", -999)
                y = obj.get("y", -999)
                return (x // 40, y // 40)

            # Extract click targets from last 6 actions (ignoring non-clicks)
            recent_clicks = []
            for h in history[-6:]:
                bucket = extract_click_bucket(h["assistant"])
                if bucket is not None:
                    recent_clicks.append(bucket)

            # If 2+ of the recent clicks target the same area, we're stuck
            is_stuck = False
            if len(recent_clicks) >= 2:
                from collections import Counter
                most_common_bucket, count = Counter(recent_clicks).most_common(1)[0]
                if count >= 2:
                    is_stuck = True

            # Also check for alternating patterns (e.g., tab/enter/tab/enter)
            if not is_stuck and len(history) >= 4:
                def extract_action_key(text: str) -> tuple:
                    m = re.search(r'\{[^{}]*\}', text)
                    if not m:
                        return (text,)
                    try:
                        obj = json.loads(m.group(0))
                    except json.JSONDecodeError:
                        return (m.group(0),)
                    atype = obj.get("type", "")
                    x = obj.get("x", -999)
                    y = obj.get("y", -999)
                    return (atype, x // 40, y // 40)
                last4 = [extract_action_key(h["assistant"]) for h in history[-4:]]
                if last4[0] == last4[2] and last4[1] == last4[3]:
                    is_stuck = True

            if is_stuck:
                # Build a summary of what the AI has been doing
                recent_actions = []
                for h in history[-8:]:
                    m = re.search(r'\{[^{}]*\}', h["assistant"])
                    if m:
                        try:
                            recent_actions.append(json.loads(m.group(0)))
                        except json.JSONDecodeError:
                            pass

                action_summary = ", ".join(
                    f"{a.get('type','')}({a.get('x','')},{a.get('y','')})"
                    if 'x' in a else a.get('type','')
                    for a in recent_actions[-4:]
                )

                # Count recent waits and clicks to give context-aware advice
                recent_waits = sum(1 for a in recent_actions if a.get("type") == "wait")
                recent_click_positions = set()
                for a in recent_actions:
                    if "click" in a.get("type", ""):
                        recent_click_positions.add((a.get("x", 0) // 80, a.get("y", 0) // 80))

                # Check if the AI mentioned numbered elements in its observations
                mentions_numbered = False
                for h in history[-4:]:
                    obs = h["assistant"].lower()
                    if any(kw in obs for kw in ["hotspot", "numbered", "circle", "1-6", "1–6", "1-5", "1–5", "step 1", "step 2"]):
                        mentions_numbered = True
                        break

                if mentions_numbered:
                    stuck_warning = (
                        f"\n\nSYSTEM WARNING — YOU ARE IN A LOOP. "
                        f"Your recent actions: [{action_summary}].\n"
                        f"You have identified NUMBERED INTERACTIVE ELEMENTS on this page "
                        f"but have NOT clicked all of them. This is the blocker.\n"
                        f"MANDATORY: Click each numbered element ONE BY ONE, starting "
                        f"from number 1, then 2, then 3, etc. After clicking each one, "
                        f"read/dismiss any popup that appears, then click the next number. "
                        f"You must complete ALL of them before NEXT will work.\n"
                        f"Do NOT wait, do NOT click NEXT, do NOT click sidebar items. "
                        f"Click the numbered elements in order."
                    )
                elif recent_waits >= 2:
                    stuck_warning = (
                        f"\n\nSYSTEM WARNING — YOU ARE IN A LOOP. "
                        f"Your recent actions: [{action_summary}].\n"
                        f"You have already waited {recent_waits} times — waiting is NOT "
                        f"the solution. The page has interactive content that must be "
                        f"completed.\n"
                        f"MANDATORY: Look carefully at the page content for:\n"
                        f"  - Clickable items you haven't tried (tiles, hotspots, "
                        f"buttons, numbered elements, tabs, expandable sections)\n"
                        f"  - If you see numbered items (1, 2, 3...), click them ALL "
                        f"in order starting from 1\n"
                        f"  - Scroll down for hidden content\n"
                        f"  - Check for unanswered questions\n"
                        f"Do NOT wait again. Do NOT click sidebar items. "
                        f"Do NOT repeat any previous action."
                    )
                else:
                    stuck_warning = (
                        f"\n\nSYSTEM WARNING — YOU ARE IN A LOOP. "
                        f"Your recent actions: [{action_summary}].\n"
                        f"These actions are NOT making progress.\n"
                        f"MANDATORY: Try these in order:\n"
                        f"  1. WAIT — a video/animation may need to finish: "
                        f'use {{"type":"wait","duration":15}}\n'
                        f"  2. Look for clickable content ON THE PAGE (not sidebar): "
                        f"numbered items, tiles, hotspots, checkboxes, expand buttons. "
                        f"If you see numbered items, click them ALL in order (1,2,3...)\n"
                        f"  3. Scroll down for hidden content or controls\n"
                        f"  4. Check if there's an unanswered question on the page\n"
                        f"Do NOT click sidebar/menu items — the problem is on THIS page.\n"
                        f"Do NOT repeat any action from your recent history."
                    )

        # Detect possible misclick — clicked a button but page changed unexpectedly
        misclick_warning = ""
        if len(history) >= 2 and not stuck_warning:
            last = history[-1]["assistant"].lower()
            prev = history[-2]["assistant"].lower()
            # If last action was a click_at and the page content changed
            had_click = '"click_at"' in last or '"double_click_at"' in last
            both_observe = "observe:" in last and "observe:" in prev
            if had_click and both_observe:
                last_obs = last.split("observe:")[1][:100]
                prev_obs = prev.split("observe:")[1][:100]
                if last_obs != prev_obs:
                    misclick_warning = (
                        "\n\nNOTE: The page changed after your last click. Verify "
                        "the result matches your intent. If it did the OPPOSITE "
                        "of what you wanted (e.g. went backward, opened wrong menu), "
                        "you may have clicked an adjacent button by mistake. "
                        "Look carefully at the button labels and coordinates."
                    )

        # Detect backward navigation — if current OBSERVE closely matches
        # something from much earlier in history, the agent likely went backward
        backward_warning = ""
        if len(history) >= 5 and not stuck_warning:
            last_lower = history[-1]["assistant"].lower()
            if "observe:" in last_lower:
                last_obs = last_lower.split("observe:")[1][:200].strip()
                last_words = set(last_obs.split())
                if len(last_words) > 4:
                    # Compare against observations from 5+ steps ago
                    for h in history[:-4]:
                        h_lower = h["assistant"].lower()
                        if "observe:" in h_lower:
                            old_obs = h_lower.split("observe:")[1][:200].strip()
                            old_words = set(old_obs.split())
                            if len(old_words) > 4:
                                overlap = len(last_words & old_words) / min(len(last_words), len(old_words))
                                if overlap > 0.6:
                                    backward_warning = (
                                        "\n\nSYSTEM WARNING — YOU APPEAR TO HAVE GONE BACKWARD. "
                                        "The current screen closely matches something you saw "
                                        "much earlier. You are likely revisiting completed work.\n"
                                        "Undo this by returning to where you were — navigate "
                                        "FORWARD, not backward. Do NOT continue from this page."
                                    )
                                    break

        # current turn: screenshot + task
        prompt = (
            f"Task: {task}\n\n"
            f"Step {len(history) + 1}. Look at the screenshot. "
            f"Follow the OBSERVE/THINK/PLAN format, then output your JSON action."
            f"{stuck_warning}{misclick_warning}{backward_warning}"
        )
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{screenshot_b64}",
                },
                {
                    "type": "input_text",
                    "text": prompt,
                },
            ],
        })
        return messages

    @staticmethod
    def _parse_response(text: str) -> dict:
        cleaned = text.strip()
        json_match = re.search(r'\{[^{}]*\}', cleaned)
        if not json_match:
            raise ParseError(f"No JSON found in AI response: {text!r}")

        json_str = json_match.group(0)
        try:
            action = json.loads(json_str)
        except json.JSONDecodeError as exc:
            raise ParseError(f"Invalid JSON from AI: {json_str!r}") from exc
        if "type" not in action:
            raise ParseError(f"Missing 'type' key in action: {action}")
        return action
