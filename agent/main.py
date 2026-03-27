"""Entry point — runs the agent loop."""

import argparse
import sys
import time

from config import (
    API_BASE_URL,
    CAPTURE_DEVICE_ID,
    MAX_STEPS,
    MODEL,
    SCREEN_HEIGHT,
    SCREEN_WIDTH,
    SERIAL_BAUD,
    SERIAL_PORT,
)
from screen import ScreenCapture
from claude_client import ClaudeClient
from hid_publisher import HIDPublisher


def home_mouse(hid: HIDPublisher) -> None:
    """Move mouse to top-left corner so Claude has a known starting position."""
    print("[Agent] Homing mouse to top-left …")
    hid.send({"type": "move", "dx": -5000, "dy": -5000})
    time.sleep(0.3)


def run(task: str, max_steps: int, device_id: int, dry_run: bool) -> None:
    screen = ScreenCapture(device_id, SCREEN_WIDTH, SCREEN_HEIGHT)
    claude = ClaudeClient(API_BASE_URL, MODEL, SCREEN_WIDTH, SCREEN_HEIGHT)

    hid = None
    if not dry_run:
        hid = HIDPublisher(SERIAL_PORT, SERIAL_BAUD)
        hid.connect()
        home_mouse(hid)

    history: list[dict] = []

    try:
        for step in range(1, max_steps + 1):
            print(f"\n=== Step {step}/{max_steps} ===")

            img = screen.grab()
            b64 = screen.to_b64(img)

            action = claude.decide(task, b64, history)
            action_type = action.get("type", "unknown")
            print(f"[Claude] action={action}")

            # keep rolling text history (no images)
            history.append(
                {
                    "user": f"Task: {task} (step {step})",
                    "assistant": str(action),
                }
            )

            if action_type == "done":
                print(f"\n[Agent] Done — {action.get('message', '')}")
                break

            if action_type == "screenshot":
                print("[Agent] Screenshot requested, no HID sent.")
                continue

            if hid and not dry_run:
                acked = hid.send(action)
                if acked:
                    print("[Agent] ESP32 ACK received")
                else:
                    print("[Agent] WARNING: no ACK from ESP32")

            time.sleep(10)  # let the screen update

        else:
            print(f"\n[Agent] Reached max steps ({max_steps}).")

    except KeyboardInterrupt:
        print("\n[Agent] Interrupted by user.")
    finally:
        cost = claude.get_cost()
        print(f"\n[Cost] Final session cost: ${cost['total_cost']:.4f}")
        print(f"  input:  {cost['input_tokens']} tokens (${cost['input_cost']:.4f})")
        print(f"  output: {cost['output_tokens']} tokens (${cost['output_cost']:.4f})")
        screen.release()
        if hid:
            hid.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="ESP32 HID AI Agent")
    parser.add_argument("--task", required=True, help="Task for the agent to perform")
    parser.add_argument("--max-steps", type=int, default=MAX_STEPS)
    parser.add_argument("--device-id", type=int, default=CAPTURE_DEVICE_ID)
    parser.add_argument("--dry-run", action="store_true", help="Skip HID sends")
    args = parser.parse_args()

    run(args.task, args.max_steps, args.device_id, args.dry_run)


if __name__ == "__main__":
    main()
