"""Tests for screen capture (requires USB capture card connected)."""

import base64
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))

from screen import ScreenCapture
from config import CAPTURE_DEVICE_ID, SCREEN_WIDTH, SCREEN_HEIGHT


@pytest.mark.hardware
class TestScreenCapture:
    def test_grab_frames(self):
        cap = ScreenCapture(CAPTURE_DEVICE_ID, SCREEN_WIDTH, SCREEN_HEIGHT)
        try:
            for _ in range(3):
                img = cap.grab()
                assert img is not None
                assert img.size == (SCREEN_WIDTH, SCREEN_HEIGHT)
        finally:
            cap.release()

    def test_to_b64(self):
        cap = ScreenCapture(CAPTURE_DEVICE_ID, SCREEN_WIDTH, SCREEN_HEIGHT)
        try:
            img = cap.grab()
            b64 = ScreenCapture.to_b64(img)
            assert isinstance(b64, str)
            assert len(b64) > 0
            # verify it decodes cleanly
            raw = base64.b64decode(b64)
            assert raw[:4] == b"\x89PNG"
        finally:
            cap.release()
