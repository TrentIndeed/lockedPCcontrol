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

        # stuck state — set by _build_input, read by caller for learning
        self.was_stuck: bool = False
        self.stuck_context: str = ""  # OBSERVE text when stuck started

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

    def _build_input(
        self, task: str, screenshot_b64: str, history: list[dict],
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
        self.was_stuck = False
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

            # Extract click targets from last 8 actions (ignoring non-clicks)
            recent_clicks = []
            for h in history[-8:]:
                bucket = extract_click_bucket(h["assistant"])
                if bucket is not None:
                    recent_clicks.append(bucket)

            # Count unique click positions — if clicking DIFFERENT spots,
            # the AI is exploring (e.g. clicking hotspots 1,2,3) not stuck
            unique_positions = len(set(recent_clicks))

            # Stuck = 3+ clicks in the same 40px bucket (truly repeating)
            is_stuck = False
            if len(recent_clicks) >= 3:
                from collections import Counter
                most_common_bucket, count = Counter(recent_clicks).most_common(1)[0]
                if count >= 3:
                    is_stuck = True

            # Also check for alternating patterns (e.g., click A / click B / click A / click B)
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

            # Also detect: no page change after 4+ actions (NEXT keeps failing)
            if not is_stuck and len(history) >= 4:
                recent_obs = []
                for h in history[-4:]:
                    obs_lower = h["assistant"].lower()
                    if "observe:" in obs_lower:
                        recent_obs.append(set(obs_lower.split("observe:")[1][:150].split()))
                if len(recent_obs) >= 3:
                    # If all recent observations share >60% words, page isn't changing
                    pairs_similar = 0
                    for i in range(len(recent_obs) - 1):
                        overlap = len(recent_obs[i] & recent_obs[i+1]) / max(len(recent_obs[i] | recent_obs[i+1]), 1)
                        if overlap > 0.6:
                            pairs_similar += 1
                    if pairs_similar >= 2:
                        is_stuck = True

            # Expose stuck state to caller
            self.was_stuck = is_stuck
            if is_stuck:
                # Capture context for learning
                for h in reversed(history[-4:]):
                    obs_lower = h["assistant"].lower()
                    if "observe:" in obs_lower:
                        self.stuck_context = obs_lower.split("observe:")[1][:200].strip()
                        break

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

                # Analyze what the AI has been trying
                recent_waits = sum(1 for a in recent_actions if a.get("type") == "wait")
                recent_click_count = sum(1 for a in recent_actions if "click" in a.get("type", ""))
                recent_click_unique = len(set(
                    (a.get("x", 0) // 60, a.get("y", 0) // 60)
                    for a in recent_actions if "click" in a.get("type", "")
                ))

                # If the AI has been clicking many DIFFERENT positions
                # (e.g. clicking hotspots), it may be done and should try NEXT
                clicking_varied_spots = recent_click_count >= 4 and recent_click_unique >= 3

                if clicking_varied_spots:
                    stuck_warning = (
                        f"\n\nSYSTEM WARNING — YOU ARE IN A LOOP. "
                        f"Your recent actions: [{action_summary}].\n"
                        f"You have been clicking multiple different elements on this page "
                        f"but the page still hasn't advanced.\n"
                        f"You may have ALREADY completed all the interactive content. "
                        f"Try clicking NEXT now to advance. If NEXT doesn't work, "
                        f"look for a 'Submit', 'Continue', or 'Close' button you missed, "
                        f"or scroll down for hidden content."
                    )
                elif recent_waits >= 2:
                    stuck_warning = (
                        f"\n\nSYSTEM WARNING — YOU ARE IN A LOOP. "
                        f"Your recent actions: [{action_summary}].\n"
                        f"You have already waited {recent_waits} times — waiting is NOT "
                        f"the solution. The page has interactive content that must be "
                        f"completed.\n"
                        f"MANDATORY: Look carefully at the page for:\n"
                        f"  - Clickable items you haven't tried (numbered items, tiles, "
                        f"hotspots, buttons, tabs, expandable sections)\n"
                        f"  - If numbered items exist, click ALL of them in order\n"
                        f"  - Scroll down for hidden content\n"
                        f"Do NOT wait again. Do NOT repeat any previous action."
                    )
                else:
                    stuck_warning = (
                        f"\n\nSYSTEM WARNING — YOU ARE IN A LOOP. "
                        f"Your recent actions: [{action_summary}].\n"
                        f"These actions are NOT making progress.\n"
                        f"MANDATORY: Try something different:\n"
                        f"  1. If there are numbered/interactive items on the page you "
                        f"haven't clicked, click them ALL in order (1,2,3...)\n"
                        f"  2. If you've already clicked all interactive items, try NEXT\n"
                        f"  3. WAIT ONCE if a video/animation may be playing: "
                        f'use {{"type":"wait","duration":15}}\n'
                        f"  4. Scroll down for hidden content or controls\n"
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
