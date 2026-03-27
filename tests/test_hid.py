"""Tests for HID publisher (requires ESP32 connected on serial)."""

import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))

from hid_publisher import HIDPublisher
from config import SERIAL_PORT, SERIAL_BAUD

ACTIONS = [
    {"type": "move", "dx": 10, "dy": 0},
    {"type": "click", "button": "left"},
    {"type": "right_click", "button": "right"},
    {"type": "double_click", "button": "left"},
    {"type": "scroll", "dx": 0, "dy": -3},
    {"type": "key", "keys": ["ctrl", "c"]},
    {"type": "type", "text": "test"},
]


@pytest.mark.hardware
class TestHIDPublisher:
    def test_all_action_types_acked(self):
        hid = HIDPublisher(SERIAL_PORT, SERIAL_BAUD)
        hid.connect()
        try:
            for action in ACTIONS:
                acked = hid.send(action, timeout=3.0)
                assert acked, f"No ACK for action type: {action['type']}"
        finally:
            hid.disconnect()
