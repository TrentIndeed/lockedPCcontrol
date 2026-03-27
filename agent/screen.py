"""Screen capture via USB capture card (appears as webcam)."""

import base64
import io
from typing import Optional

import cv2
import numpy as np
from PIL import Image


class ScreenCaptureError(Exception):
    pass


class ScreenCapture:
    def __init__(self, device_id: int, width: int = 1280, height: int = 720) -> None:
        self._width = width
        self._height = height
        self._cap = cv2.VideoCapture(device_id)
        if not self._cap.isOpened():
            raise ScreenCaptureError(
                f"Failed to open capture device {device_id}. "
                "Check that the USB capture card is connected."
            )

    def grab(self) -> Image.Image:
        ret, frame = self._cap.read()
        if not ret or frame is None:
            raise ScreenCaptureError("Failed to read frame from capture device.")
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        img = img.resize((self._width, self._height), Image.LANCZOS)
        return img

    @staticmethod
    def to_b64(image: Image.Image) -> str:
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")

    def release(self) -> None:
        if self._cap is not None:
            self._cap.release()
