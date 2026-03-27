"""Claude client — talks to a local OpenAI-compatible proxy at 127.0.0.1:10531."""

import json
import re

from openai import OpenAI

SYSTEM_PROMPT = """\
You are an AI agent controlling a remote PC through physical HID input.
The screen is {w}x{h} pixels. You see a screenshot each turn.

You control the mouse and keyboard via JSON actions. Mouse movement is
RELATIVE (dx/dy deltas from current position). You cannot jump to absolute
coordinates — move incrementally and take screenshots to verify position.

Available actions (reply with exactly ONE raw JSON object, nothing else):

  {{"type":"move","dx":<int>,"dy":<int>}}
  {{"type":"click","button":"left"}}
  {{"type":"right_click","button":"right"}}
  {{"type":"double_click","button":"left"}}
  {{"type":"scroll","dx":0,"dy":<int>}}       (negative = scroll down)
  {{"type":"key","keys":["ctrl","c"]}}
  {{"type":"type","text":"hello world"}}
  {{"type":"screenshot"}}                     (no-op; just get a fresh screenshot)
  {{"type":"done","message":"<reason>"}}

Rules:
- Reply with RAW JSON only. No markdown, no explanation, no extra text.
- One action per turn.
- If the task is complete, reply with the "done" action.
- If you need to see the screen again without acting, use "screenshot".
- For key combos: list modifier keys first, then the main key.
  Supported keys: ctrl, alt, shift, win, tab, enter, escape, backspace,
  delete, up, down, left, right, f1-f12, home, end, pageup, pagedown, space,
  plus any single printable character.
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
        self._client = OpenAI(base_url=base_url, api_key="not-needed")
        self._model = model
        self._system = SYSTEM_PROMPT.format(w=screen_w, h=screen_h)

        # cost tracking
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0

    # -- public API ----------------------------------------------------------

    def decide(self, task: str, screenshot_b64: str, history: list[dict]) -> dict:
        messages = self._build_messages(task, screenshot_b64, history)
        response = self._client.chat.completions.create(
            model=self._model,
            max_tokens=256,
            messages=messages,
        )

        # accumulate token usage
        if response.usage:
            self.total_input_tokens += response.usage.prompt_tokens
            self.total_output_tokens += response.usage.completion_tokens
        self._print_cost()

        raw_text = response.choices[0].message.content
        return self._parse_response(raw_text)

    def get_cost(self) -> dict[str, float]:
        input_cost = (self.total_input_tokens / 1_000_000) * 3.00
        output_cost = (self.total_output_tokens / 1_000_000) * 15.00
        return {
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "input_cost": input_cost,
            "output_cost": output_cost,
            "total_cost": input_cost + output_cost,
        }

    # -- internals -----------------------------------------------------------

    def _print_cost(self) -> None:
        c = self.get_cost()
        print(
            f"[Cost] in={c['input_tokens']} out={c['output_tokens']} "
            f"session=${c['total_cost']:.4f}"
        )

    def _build_messages(
        self, task: str, screenshot_b64: str, history: list[dict]
    ) -> list[dict]:
        messages: list[dict] = [
            {"role": "system", "content": self._system},
        ]

        # rolling history (text only, no images)
        for entry in history[-10:]:
            messages.append({"role": "user", "content": entry["user"]})
            messages.append({"role": "assistant", "content": entry["assistant"]})

        # current turn: screenshot + task reminder
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{screenshot_b64}",
                        },
                    },
                    {
                        "type": "text",
                        "text": f"Task: {task}\nDecide the next single action.",
                    },
                ],
            }
        )
        return messages

    @staticmethod
    def _parse_response(text: str) -> dict:
        cleaned = text.strip()
        # strip accidental markdown fences
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip()
        try:
            action = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ParseError(f"Invalid JSON from Claude: {text!r}") from exc
        if "type" not in action:
            raise ParseError(f"Missing 'type' key in action: {action}")
        return action
