"""Tests for the agent loop — no hardware required."""

import sys
import os
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))


def _blank_image():
    return Image.new("RGB", (1280, 720), color=(0, 0, 0))


class TestAgentLoop:
    def test_loop_sends_hid_and_stops_on_done(self):
        actions = [
            {"type": "click", "button": "left"},
            {"type": "move", "dx": 100, "dy": 50},
            {"type": "type", "text": "hello"},
            {"type": "done", "message": "task complete"},
        ]

        with (
            patch("main.ScreenCapture") as MockScreen,
            patch("main.HIDPublisher") as MockHID,
            patch("main.ClaudeClient") as MockClaude,
        ):
            mock_screen = MockScreen.return_value
            mock_screen.grab.return_value = _blank_image()
            mock_screen.to_b64.return_value = "AAAA"

            mock_hid = MockHID.return_value
            mock_hid.send.return_value = True

            mock_claude = MockClaude.return_value
            mock_claude.decide.side_effect = actions
            mock_claude.get_cost.return_value = {
                "input_tokens": 0,
                "output_tokens": 0,
                "input_cost": 0.0,
                "output_cost": 0.0,
                "total_cost": 0.0,
            }

            from main import run

            run("test task", max_steps=10, device_id=0, dry_run=False)

            # HID should have been called for click, move, type (not done)
            assert mock_hid.send.call_count == 3

    def test_loop_skips_hid_on_screenshot(self):
        actions = [
            {"type": "screenshot"},
            {"type": "click", "button": "left"},
            {"type": "done", "message": "ok"},
        ]

        with (
            patch("main.ScreenCapture") as MockScreen,
            patch("main.HIDPublisher") as MockHID,
            patch("main.ClaudeClient") as MockClaude,
        ):
            mock_screen = MockScreen.return_value
            mock_screen.grab.return_value = _blank_image()
            mock_screen.to_b64.return_value = "AAAA"

            mock_hid = MockHID.return_value
            mock_hid.send.return_value = True

            mock_claude = MockClaude.return_value
            mock_claude.decide.side_effect = actions
            mock_claude.get_cost.return_value = {
                "input_tokens": 0,
                "output_tokens": 0,
                "input_cost": 0.0,
                "output_cost": 0.0,
                "total_cost": 0.0,
            }

            from main import run

            run("test task", max_steps=10, device_id=0, dry_run=False)

            # only the click should have been sent, not the screenshot
            assert mock_hid.send.call_count == 1

    def test_loop_respects_max_steps(self):
        # always return a move action — should stop at max_steps
        with (
            patch("main.ScreenCapture") as MockScreen,
            patch("main.HIDPublisher") as MockHID,
            patch("main.ClaudeClient") as MockClaude,
        ):
            mock_screen = MockScreen.return_value
            mock_screen.grab.return_value = _blank_image()
            mock_screen.to_b64.return_value = "AAAA"

            mock_hid = MockHID.return_value
            mock_hid.send.return_value = True

            mock_claude = MockClaude.return_value
            mock_claude.decide.return_value = {"type": "move", "dx": 1, "dy": 1}
            mock_claude.get_cost.return_value = {
                "input_tokens": 0,
                "output_tokens": 0,
                "input_cost": 0.0,
                "output_cost": 0.0,
                "total_cost": 0.0,
            }

            from main import run

            run("test task", max_steps=5, device_id=0, dry_run=False)

            assert mock_claude.decide.call_count == 5
