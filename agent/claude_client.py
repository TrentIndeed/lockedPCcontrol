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
        self._system = prompt.format(w=screen_w, h=screen_h)

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

        # stuck detection — fuzzy: same action type + nearby coordinates
        stuck_warning = ""
        if len(history) >= 3:
            def extract_action_key(text: str) -> tuple:
                """Return (type, x_bucket, y_bucket) for fuzzy comparison."""
                m = re.search(r'\{[^{}]*\}', text)
                if not m:
                    return (text,)
                try:
                    obj = json.loads(m.group(0))
                except json.JSONDecodeError:
                    return (m.group(0),)
                atype = obj.get("type", "")
                # bucket coordinates to nearest 30px so slight variations match
                x = obj.get("x", -999)
                y = obj.get("y", -999)
                return (atype, x // 30, y // 30)

            last_keys = [extract_action_key(h["assistant"]) for h in history[-3:]]
            # Also check for alternating patterns (e.g., tab/enter/tab/enter)
            is_stuck = len(set(last_keys)) == 1
            if not is_stuck and len(history) >= 4:
                last4 = [extract_action_key(h["assistant"]) for h in history[-4:]]
                # A-B-A-B pattern: positions 0,2 match AND positions 1,3 match
                if last4[0] == last4[2] and last4[1] == last4[3]:
                    is_stuck = True
            if is_stuck:
                stuck_warning = (
                    "\n\nWARNING: You repeated the same action 3+ times with no "
                    "effect. Try a COMPLETELY DIFFERENT approach:\n"
                    "  - Click 20-30px INWARD from the screen edge\n"
                    "  - Scroll the page so the button moves away from the edge\n"
                    "  - Use arrow keys (right/left) to navigate controls\n"
                    "  - Look for an alternative path to the same goal\n"
                    "Do NOT use Tab (it triggers skip-navigation overlays).\n"
                    "Do NOT repeat the same action again."
                )

        # current turn: screenshot + task
        prompt = (
            f"Task: {task}\n\n"
            f"Step {len(history) + 1}. Look at the screenshot. "
            f"Follow the OBSERVE/THINK/PLAN format, then output your JSON action."
            f"{stuck_warning}"
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
