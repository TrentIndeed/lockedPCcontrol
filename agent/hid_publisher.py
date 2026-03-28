"""Serial publisher — sends HID commands to the ESP32 over USB serial."""

import json
import time

import serial


class HIDPublisher:
    def __init__(self, port: str, baud: int) -> None:
        self._port = port
        self._baud = baud
        self._ser: serial.Serial | None = None

    def connect(self) -> None:
        self._ser = serial.Serial(self._port, self._baud, timeout=2)
        time.sleep(2)  # wait for ESP32 to reset after serial open
        # drain any boot messages
        self._ser.reset_input_buffer()
        print(f"[Serial] Connected to {self._port} @ {self._baud}")

    def send(self, action: dict, timeout: float = 2.0) -> bool:
        if not self._ser:
            return False
        payload = json.dumps(action) + "\n"
        self._ser.write(payload.encode("utf-8"))
        self._ser.flush()

        # wait for ACK line from ESP32
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._ser.in_waiting:
                line = self._ser.readline().decode("utf-8", errors="replace").strip()
                if line.startswith("{"):
                    try:
                        ack = json.loads(line)
                        if ack.get("status") == "ok":
                            return True
                    except json.JSONDecodeError:
                        pass
            time.sleep(0.05)
        return False

    def send_nowait(self, action: dict) -> None:
        """Send a command without waiting for ACK — used for rapid small moves."""
        if not self._ser:
            return
        payload = json.dumps(action) + "\n"
        self._ser.write(payload.encode("utf-8"))
        self._ser.flush()

    def drain(self) -> None:
        """Drain any pending ACK responses from the serial buffer."""
        if self._ser:
            time.sleep(0.1)
            self._ser.reset_input_buffer()

    def disconnect(self) -> None:
        if self._ser:
            self._ser.close()
            print("[Serial] Disconnected")
