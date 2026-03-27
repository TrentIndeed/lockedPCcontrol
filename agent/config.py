"""Configuration — loads from environment / .env file."""

import os
from dotenv import load_dotenv

load_dotenv()

API_BASE_URL: str = os.environ.get("API_BASE_URL", "http://127.0.0.1:10531/v1")
SERIAL_PORT: str = os.environ.get("SERIAL_PORT", "COM5")
SERIAL_BAUD: int = int(os.environ.get("SERIAL_BAUD", "115200"))
CAPTURE_DEVICE_ID: int = int(os.environ.get("CAPTURE_DEVICE_ID", "0"))
SCREEN_WIDTH: int = 1280
SCREEN_HEIGHT: int = 720
MAX_STEPS: int = 50
MODEL: str = "claude-sonnet-4-6"
