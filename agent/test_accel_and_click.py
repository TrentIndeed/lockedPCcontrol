"""Tests for mouse calibration and click accuracy.

Unit test (no hardware):
    python test_accel_and_click.py --unit

Live click test (requires ESP32 + capture card + AI proxy):
    python test_accel_and_click.py --live
"""

import argparse
import json
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Unit test: verify calibrate_mouse sends correct HID sequence
# ---------------------------------------------------------------------------


class MockHID:
    """Records all HID commands sent."""

    def __init__(self):
        self.commands: list[dict] = []

    def send(self, action: dict, timeout: float = 2.0) -> bool:
        self.commands.append(action)
        return True

    def connect(self):
        pass


class TestMouseScale(unittest.TestCase):
    """Verify MICKEY_SCALE defaults and move_cursor_to math."""

    def test_default_scales(self):
        import web
        self.assertAlmostEqual(web.MICKEY_SCALE_X, 0.37, places=2)
        self.assertAlmostEqual(web.MICKEY_SCALE_Y, 0.36, places=2)
        print("\n  PASS: Default scales are set correctly.")

    def test_skips_if_already_calibrated(self):
        import web
        web.calibrated = True
        web.socketio.emit = MagicMock()
        web.calibrate_mouse()
        self.assertTrue(web.calibrated)
        print("\n  PASS: Skipped (already calibrated).")

    def test_move_sends_correct_mickeys(self):
        import web
        mock_hid = MockHID()
        web.hid = mock_hid
        web.socketio.emit = MagicMock()

        with patch("time.sleep"):
            web.move_cursor_to(640, 360)

        # Should send: home (-10000x2), then move with scaled mickeys
        # dx = 640 * 0.37 = 236, dy = 360 * 0.36 = 129
        move_cmd = mock_hid.commands[-1]  # last command is the actual move
        self.assertEqual(move_cmd["type"], "move")
        self.assertEqual(move_cmd["dx"], int(640 * web.MICKEY_SCALE_X))
        self.assertEqual(move_cmd["dy"], int(360 * web.MICKEY_SCALE_Y))
        print(f"\n  move_cursor_to(640,360) -> dx={move_cmd['dx']}, dy={move_cmd['dy']}")
        print("  PASS: Mickeys computed correctly.")


# ---------------------------------------------------------------------------
# Live test: calibration + click accuracy
# ---------------------------------------------------------------------------

def run_live_click_test():
    """Calibrate mouse, then test clicking UI elements on the locked PC."""
    from config import (
        API_BASE_URL, CAPTURE_DEVICE_ID, MODEL,
        SCREEN_WIDTH, SCREEN_HEIGHT, SERIAL_PORT, SERIAL_BAUD,
        TARGET_WIDTH, TARGET_HEIGHT,
    )
    from screen import ScreenCapture
    from claude_client import ClaudeClient
    from hid_publisher import HIDPublisher

    print("\n=== Live Click Accuracy Test ===")
    print(f"  Screen: {SCREEN_WIDTH}x{SCREEN_HEIGHT} (image) -> {TARGET_WIDTH}x{TARGET_HEIGHT} (real)")

    # Connect hardware
    screen = ScreenCapture(CAPTURE_DEVICE_ID, SCREEN_WIDTH, SCREEN_HEIGHT)
    hid = HIDPublisher(SERIAL_PORT, SERIAL_BAUD)
    hid.connect()
    ai = ClaudeClient(API_BASE_URL, MODEL, SCREEN_WIDTH, SCREEN_HEIGHT)

    # Wire up web module
    import web
    web.screen = screen
    web.hid = hid
    web.claude = ai
    web.socketio.emit = MagicMock()

    # Step 1: Use hardcoded scale (skip calibration)
    print(f"\n  Step 1: Using hardcoded scale X={web.MICKEY_SCALE_X:.3f} Y={web.MICKEY_SCALE_Y:.3f}")
    time.sleep(1)

    # Step 2: Open Notepad and maximize it so it's guaranteed in front
    print("  Step 2: Opening Notepad...")
    hid.send({"type": "key", "keys": ["win", "r"]})
    time.sleep(2.0)
    hid.send({"type": "type", "text": "notepad"})
    time.sleep(0.5)
    hid.send({"type": "key", "keys": ["enter"]})
    time.sleep(3.0)
    # Alt+Space, then X to maximize — ensures it's in front and fullscreen
    hid.send({"type": "key", "keys": ["alt", "space"]})
    time.sleep(0.5)
    hid.send({"type": "key", "keys": ["x"]})
    time.sleep(1.0)

    # Save debug screenshot
    img_debug = screen.grab()
    img_debug.save("test_debug_screenshot.png")
    print("  Saved debug screenshot to test_debug_screenshot.png")

    # Step 3: Ask AI where Notepad is
    print("  Step 3: Finding Notepad...")
    ai.reset()
    img = screen.grab()
    b64 = ScreenCapture.to_b64(img)

    result = ai.decide(
        "Look at the screenshot. Is there a Notepad window visible? "
        "If yes, reply with JSON: {\"type\":\"done\",\"message\":\"visible\",\"x\":<center_x>,\"y\":<title_bar_y>}. "
        "If not visible, reply: {\"type\":\"done\",\"message\":\"not visible\"}",
        b64, [],
    )
    print(f"  AI response: {result}")

    if result.get("message") != "visible":
        print("  FAIL: Notepad not visible on screen.")
        hid.disconnect()
        screen.release()
        return False

    target_x = result.get("x", SCREEN_WIDTH // 2)
    target_y = result.get("y", 30)
    print(f"  Notepad title bar at ({target_x}, {target_y})")

    # Step 4: Click title bar
    print(f"  Step 4: Clicking at ({target_x}, {target_y})...")
    web.move_cursor_to(target_x, target_y)
    hid.send({"type": "click", "button": "left"})
    time.sleep(1)

    # Step 5: Verify click
    print("  Step 5: Verifying click accuracy...")
    img2 = screen.grab()
    b64_2 = ScreenCapture.to_b64(img2)
    img2.save("test_after_click.png")

    result2 = ai.decide(
        "Look at the screenshot. Is the Notepad window focused (active, title bar highlighted)? "
        "Also, can you see the mouse cursor? If so, where approximately is it? "
        "Reply JSON: {\"type\":\"done\",\"message\":\"focused\" or \"not focused\",\"cursor_x\":<x>,\"cursor_y\":<y>}",
        b64_2, [],
    )
    print(f"  AI response: {result2}")

    focused = "focused" in str(result2.get("message", "")).lower() and "not" not in str(result2.get("message", "")).lower()

    # Step 6: Click center of text area to verify mid-screen accuracy
    print("  Step 6: Clicking center of Notepad text area (640, 360)...")
    web.move_cursor_to(640, 360)
    hid.send({"type": "click", "button": "left"})
    time.sleep(0.5)
    # Type a marker character
    hid.send({"type": "type", "text": "X"})
    time.sleep(0.5)

    img3 = screen.grab()
    b64_3 = ScreenCapture.to_b64(img3)
    img3.save("test_after_center_click.png")

    result3 = ai.decide(
        "Look at the Notepad window. Is there a letter 'X' typed in the text area? "
        "If yes, where approximately is it (report coordinates)? "
        "Reply: {\"type\":\"done\",\"message\":\"found\" or \"not found\",\"x\":<x>,\"y\":<y>}",
        b64_3, [],
    )
    print(f"  AI response: {result3}")

    center_ok = result3.get("message") == "found"

    # Step 7: Close with Alt+F4 and verify
    print("  Step 7: Closing Notepad with Alt+F4...")
    hid.send({"type": "key", "keys": ["alt", "f4"]})
    time.sleep(1)
    # "Don't Save" dialog
    hid.send({"type": "key", "keys": ["tab"]})
    time.sleep(0.2)
    hid.send({"type": "key", "keys": ["enter"]})
    time.sleep(1)

    success = focused and center_ok

    print(f"\n  Results:")
    print(f"    Title bar click focused Notepad: {'YES' if focused else 'NO'}")
    print(f"    Center click typed X: {'YES' if center_ok else 'NO'}")
    print(f"    MICKEY_SCALE X={web.MICKEY_SCALE_X:.3f} Y={web.MICKEY_SCALE_Y:.3f}")
    print(f"  {'PASS' if success else 'FAIL'}")
    print(f"  Total AI cost: ${ai.get_cost()['total_cost']:.4f}")

    hid.disconnect()
    screen.release()
    return success


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mouse calibration and click tests")
    parser.add_argument("--unit", action="store_true", help="Run unit tests (no hardware)")
    parser.add_argument("--live", action="store_true", help="Run live click accuracy test")
    args = parser.parse_args()

    if not args.unit and not args.live:
        args.unit = True

    import os
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, ".")

    if args.unit:
        print("Running unit tests...")
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromTestCase(TestMouseScale)
        runner = unittest.TextTestRunner(verbosity=2)
        result = runner.run(suite)
        if not result.wasSuccessful():
            sys.exit(1)

    if args.live:
        if not run_live_click_test():
            sys.exit(1)
