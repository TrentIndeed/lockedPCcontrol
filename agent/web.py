"""Web interface — live screen view + chat to control the agent."""

import json
import os
import re
import threading
import time
from datetime import datetime

from flask import Flask, render_template
from flask_socketio import SocketIO

from config import (
    API_BASE_URL,
    CAPTURE_DEVICE_ID,
    MAX_STEPS,
    MICKEY_SCALE_X as _CFG_MICKEY_SCALE_X,
    MICKEY_SCALE_Y as _CFG_MICKEY_SCALE_Y,
    MODEL,
    PROMPT_RATE as _CFG_PROMPT_RATE,
    SCREEN_HEIGHT,
    SCREEN_WIDTH,
    SERIAL_BAUD,
    SERIAL_PORT,
    list_profiles,
    load_profile,
    add_lesson,
)
from screen import ScreenCapture
from claude_client import ClaudeClient, ParseError
from hid_publisher import HIDPublisher

app = Flask(__name__)
app.config["SECRET_KEY"] = "dev"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# --- persistent storage paths -----------------------------------------------
DATA_DIR = os.path.join(os.path.dirname(__file__), ".data")
TASK_LOG_FILE = os.path.join(DATA_DIR, "task_log.json")
NOTES_FILE = os.path.join(DATA_DIR, "session_notes.json")

os.makedirs(DATA_DIR, exist_ok=True)


def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# --- shared state -----------------------------------------------------------
agent_lock = threading.Lock()
agent_thread: threading.Thread | None = None
agent_stop = threading.Event()
guidance_queue: list[str] = []  # thread-safe via GIL for simple appends/reads
guidance_lock = threading.Lock()

screen: ScreenCapture | None = None
hid: HIDPublisher | None = None
claude: ClaudeClient | None = None

prompt_rate: float = _CFG_PROMPT_RATE
current_task: str = ""
max_steps: int = MAX_STEPS
active_profile: str = ""  # name of the active task profile (empty = none)

# persistent session data
task_log: list[dict] = _load_json(TASK_LOG_FILE, [])
session_notes: list[str] = _load_json(NOTES_FILE, [])

# mouse mickey-to-pixel scale (mickeys per image pixel) — loaded from config
MICKEY_SCALE_X = _CFG_MICKEY_SCALE_X
MICKEY_SCALE_Y = _CFG_MICKEY_SCALE_Y

# track known cursor position (image pixels) — updated by move_cursor_to
cursor_x: int = 0
cursor_y: int = 0


def save_task_log() -> None:
    _save_json(TASK_LOG_FILE, task_log)


def save_notes() -> None:
    _save_json(NOTES_FILE, session_notes)


def init_hardware() -> None:
    global screen, hid, claude
    if screen is None:
        screen = ScreenCapture(CAPTURE_DEVICE_ID, SCREEN_WIDTH, SCREEN_HEIGHT)
    if hid is None:
        hid = HIDPublisher(SERIAL_PORT, SERIAL_BAUD)
        hid.connect()
    if claude is None:
        claude = ClaudeClient(API_BASE_URL, MODEL, SCREEN_WIDTH, SCREEN_HEIGHT)


def startup_check() -> list[dict]:
    """Run hardware health checks. Returns list of {component, ok, message, fixes}."""
    results: list[dict] = []

    # --- ESP32 Serial -----------------------------------------------------------
    esp_result = {"component": "ESP32 Serial", "ok": False, "message": "", "fixes": []}
    try:
        import serial as _serial
        import serial.tools.list_ports as list_ports

        available = [p.device for p in list_ports.comports()]

        if SERIAL_PORT not in available:
            esp_result["message"] = (
                f"Port {SERIAL_PORT} not found. Available ports: {available or 'none'}"
            )
            esp_result["fixes"] = [
                "Check that the ESP32-S3 is plugged in via USB",
                f"Update 'serial_port' in agent.yaml (current: {SERIAL_PORT})",
                "Try a different USB cable — some are charge-only",
                "Open Device Manager and check COM ports",
                "Install CP210x or CH340 USB-serial drivers if the device doesn't appear",
            ]
        else:
            # Port exists — try opening and sending a no-op to get ACK
            try:
                test_ser = _serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=2)
                import time as _t
                _t.sleep(2)  # ESP32 resets on serial open
                test_ser.reset_input_buffer()

                # Send a tiny no-op move and check for ACK
                test_ser.write(b'{"type":"move","dx":0,"dy":0}\n')
                test_ser.flush()

                deadline = _t.time() + 3.0
                got_ack = False
                while _t.time() < deadline:
                    if test_ser.in_waiting:
                        line = test_ser.readline().decode("utf-8", errors="replace").strip()
                        if line.startswith("{"):
                            try:
                                ack = json.loads(line)
                                if ack.get("status") == "ok":
                                    got_ack = True
                                    break
                            except json.JSONDecodeError:
                                pass
                    _t.sleep(0.05)

                test_ser.close()

                if got_ack:
                    esp_result["ok"] = True
                    esp_result["message"] = f"Connected on {SERIAL_PORT} — ACK received"
                else:
                    esp_result["message"] = (
                        f"Port {SERIAL_PORT} opened but ESP32 did not respond (no ACK)"
                    )
                    esp_result["fixes"] = [
                        "Verify the ESP32-S3 firmware is flashed and running",
                        f"Check baud rate matches firmware (config: {SERIAL_BAUD})",
                        "Press the RST button on the ESP32-S3 and try again",
                        "Open a serial monitor to see if the ESP32 is outputting anything",
                    ]
            except _serial.SerialException as e:
                esp_result["message"] = f"Cannot open {SERIAL_PORT}: {e}"
                esp_result["fixes"] = [
                    f"Another program may have {SERIAL_PORT} open (Arduino IDE, PuTTY, etc.)",
                    "Close other serial monitors and retry",
                    "Unplug and replug the ESP32-S3",
                ]
    except ImportError:
        esp_result["message"] = "pyserial not installed"
        esp_result["fixes"] = ["Run: pip install pyserial"]
    except Exception as e:
        esp_result["message"] = f"Unexpected error: {e}"

    results.append(esp_result)

    # --- HDMI Capture Card ------------------------------------------------------
    cap_result = {"component": "HDMI Capture", "ok": False, "message": "", "fixes": []}
    try:
        import cv2 as _cv2

        cap = _cv2.VideoCapture(CAPTURE_DEVICE_ID)
        if not cap.isOpened():
            cap_result["message"] = f"Cannot open capture device {CAPTURE_DEVICE_ID}"
            cap_result["fixes"] = [
                "Check that the HDMI capture card is plugged into a USB port",
                "Check that the locked PC's HDMI output is connected to the capture card",
                f"Try a different device ID in agent.yaml (current: {CAPTURE_DEVICE_ID})",
                "Device 0 is usually the built-in webcam — try 1, 2, or 3",
                "Open Camera app or OBS to verify the capture card works",
                "Try a different USB port (USB 3.0 recommended)",
            ]
        else:
            ret, frame = cap.read()
            cap.release()

            if not ret or frame is None:
                cap_result["message"] = (
                    f"Device {CAPTURE_DEVICE_ID} opened but returned no frame"
                )
                cap_result["fixes"] = [
                    "The locked PC may be off or in sleep mode — check it has video output",
                    "Check the HDMI cable between the locked PC and the capture card",
                    "The capture card may need a moment after plugging in — wait and retry",
                    "Try a different HDMI cable",
                ]
            else:
                h, w = frame.shape[:2]
                import numpy as _np
                mean_brightness = float(_np.mean(frame))
                if mean_brightness < 5.0:
                    cap_result["message"] = (
                        f"Device {CAPTURE_DEVICE_ID} returns frames ({w}x{h}) "
                        f"but image is black (brightness={mean_brightness:.1f}) — no signal?"
                    )
                    cap_result["fixes"] = [
                        "The locked PC screen may be off, asleep, or showing a black screen",
                        "Check the HDMI cable is firmly connected at both ends",
                        "Move the mouse or press a key on the locked PC to wake it",
                        "Try a different HDMI cable or port on the locked PC",
                    ]
                else:
                    cap_result["ok"] = True
                    cap_result["message"] = (
                        f"Capturing {w}x{h} frames from device {CAPTURE_DEVICE_ID} "
                        f"(brightness={mean_brightness:.0f})"
                    )
    except ImportError:
        cap_result["message"] = "opencv-python not installed"
        cap_result["fixes"] = ["Run: pip install opencv-python"]
    except Exception as e:
        cap_result["message"] = f"Unexpected error: {e}"

    results.append(cap_result)
    return results


calibrated = False

# Calibration constants
_CAL_MICKEYS_SHORT = 200   # first probe distance (mickeys)
_CAL_MICKEYS_LONG = 400    # second probe distance (2x short)
_CAL_Y_OFFSET = 80         # mickeys down from top to avoid corner menus
_ACCEL_RATIO_TOL = 0.15    # max deviation from 2.0 before flagging acceleration
_CAL_DIFF_THRESHOLD = 30   # pixel intensity change to count as "different"
_CAL_MIN_CHANGED = 200     # minimum changed pixels to consider a context menu detected


class CalibrationError(Exception):
    """Raised when calibration detects a problem."""
    pass


def _detect_cursor_via_context_menu(mickeys_x: int) -> tuple[int, int]:
    """Move cursor by mickeys_x from home, right-click, and detect position
    by finding where the context menu appeared via screenshot diff.

    Returns (x, y) pixel position of the context menu top-left corner,
    which is where the cursor was when right-clicked.
    """
    import numpy as np

    # Dismiss any existing popup
    hid.send({"type": "key", "keys": ["escape"]})
    time.sleep(0.3)

    # Baseline screenshot (no context menu)
    before = np.array(screen.grab())

    # Home, move to test position, right-click
    _home_cursor()
    hid.send({"type": "move", "dx": mickeys_x, "dy": _CAL_Y_OFFSET})
    time.sleep(0.2)
    hid.send({"type": "right_click", "button": "right"})
    time.sleep(0.5)

    # Screenshot with context menu
    after = np.array(screen.grab())

    # Close context menu
    hid.send({"type": "key", "keys": ["escape"]})
    time.sleep(0.3)

    # Diff to find context menu position
    diff = np.abs(before.astype(np.int16) - after.astype(np.int16))
    diff_gray = diff.sum(axis=2)
    changed = diff_gray > _CAL_DIFF_THRESHOLD

    num_changed = int(changed.sum())
    if num_changed < _CAL_MIN_CHANGED:
        raise CalibrationError(
            f"No context menu detected ({num_changed} changed pixels, "
            f"need {_CAL_MIN_CHANGED}). Try minimizing windows first."
        )

    # Find top-left corner of the changed region = cursor position
    rows = np.any(changed, axis=1)
    cols = np.any(changed, axis=0)
    top = int(np.argmax(rows))
    left = int(np.argmax(cols))

    return (left, top)


def calibrate_mouse() -> None:
    """Detect mouse acceleration and calibrate MICKEY_SCALE.

    Minimizes to desktop first (Win+D) so right-click context menus don't
    interfere with fullscreen apps. Restores windows when done.
    """
    global calibrated, MICKEY_SCALE_X, MICKEY_SCALE_Y

    if calibrated:
        socketio.emit("log", {"text": "[Calibration] Already calibrated. Skipping."})
        return

    init_hardware()
    socketio.emit("log", {"text": "[Calibration] Starting — switching to desktop..."})

    # Minimize everything to desktop so right-click is safe
    hid.send({"type": "key", "keys": ["win", "d"]})
    time.sleep(1.5)

    try:
        _run_calibration_probes()
    finally:
        # Always restore windows, even if calibration fails
        socketio.emit("log", {"text": "[Calibration] Restoring windows..."})
        hid.send({"type": "key", "keys": ["win", "d"]})
        time.sleep(1.0)


def _run_calibration_probes() -> None:
    """Run the actual calibration probes (called while on desktop)."""
    global calibrated, MICKEY_SCALE_X, MICKEY_SCALE_Y

    # --- Probe 1: short distance -------------------------------------------
    try:
        x1, y1 = _detect_cursor_via_context_menu(_CAL_MICKEYS_SHORT)
    except (CalibrationError, Exception) as e:
        socketio.emit("log", {"text": f"[Calibration] WARN: Short probe failed: {e}"})
        socketio.emit("log", {"text": f"[Calibration] Using config scale X={MICKEY_SCALE_X:.4f} Y={MICKEY_SCALE_Y:.4f}"})
        calibrated = True
        return

    ppm_short = x1 / _CAL_MICKEYS_SHORT
    socketio.emit("log", {
        "text": f"[Calibration] Short: {_CAL_MICKEYS_SHORT} mickeys -> ({x1}, {y1}), ppm_x={ppm_short:.3f}"
    })

    # --- Probe 2: long distance --------------------------------------------
    try:
        x2, y2 = _detect_cursor_via_context_menu(_CAL_MICKEYS_LONG)
    except (CalibrationError, Exception) as e:
        socketio.emit("log", {"text": f"[Calibration] WARN: Long probe failed: {e}"})
        socketio.emit("log", {"text": f"[Calibration] Using config scale X={MICKEY_SCALE_X:.4f} Y={MICKEY_SCALE_Y:.4f}"})
        calibrated = True
        return

    ppm_long = x2 / _CAL_MICKEYS_LONG
    socketio.emit("log", {
        "text": f"[Calibration] Long: {_CAL_MICKEYS_LONG} mickeys -> ({x2}, {y2}), ppm_x={ppm_long:.3f}"
    })

    # --- Acceleration check ------------------------------------------------
    if x1 > 0:
        actual_ratio = x2 / x1
        expected_ratio = _CAL_MICKEYS_LONG / _CAL_MICKEYS_SHORT  # 2.0
        deviation = abs(actual_ratio - expected_ratio) / expected_ratio

        socketio.emit("log", {
            "text": f"[Calibration] Ratio: {actual_ratio:.2f} (expected {expected_ratio:.1f}, deviation {deviation:.1%})"
        })

        if deviation > _ACCEL_RATIO_TOL:
            msg = (
                f"MOUSE ACCELERATION DETECTED! Distance ratio is {actual_ratio:.2f} "
                f"(expected {expected_ratio:.1f}, {deviation:.0%} off). "
                f"Disable 'Enhance pointer precision' in the locked PC's mouse settings "
                f"(Control Panel -> Mouse -> Pointer Options -> uncheck 'Enhance pointer precision')."
            )
            socketio.emit("log", {"text": f"[Calibration] ERROR: {msg}"})
            socketio.emit("calibration_error", {"error": "acceleration", "message": msg})
            raise CalibrationError(msg)

    # --- Derive and apply scale --------------------------------------------
    avg_ppm_x = (ppm_short + ppm_long) / 2
    avg_ppm_y = ((y1 / _CAL_Y_OFFSET) + (y2 / _CAL_Y_OFFSET)) / 2

    new_scale_x = 1.0 / avg_ppm_x if avg_ppm_x > 0 else MICKEY_SCALE_X
    new_scale_y = 1.0 / avg_ppm_y if avg_ppm_y > 0 else MICKEY_SCALE_Y

    socketio.emit("log", {
        "text": f"[Calibration] Scale: x={MICKEY_SCALE_X:.4f}->{new_scale_x:.4f}, "
                f"y={MICKEY_SCALE_Y:.4f}->{new_scale_y:.4f}"
    })

    MICKEY_SCALE_X = new_scale_x
    MICKEY_SCALE_Y = new_scale_y

    socketio.emit("log", {
        "text": f"[Calibration] PASS. Scale X={MICKEY_SCALE_X:.4f} Y={MICKEY_SCALE_Y:.4f}"
    })
    calibrated = True


def build_session_context() -> str:
    parts: list[str] = []
    if session_notes:
        parts.append("Session notes from the user:")
        for note in session_notes:
            parts.append(f"  - {note}")
    if task_log:
        parts.append(f"\nPrevious tasks this session ({len(task_log)} total):")
        for entry in task_log[-5:]:
            status = entry.get("status", "unknown")
            parts.append(f"  - [{entry['time']}] \"{entry['task']}\" — {status}")
    return "\n".join(parts)


def stream_screen() -> None:
    while not agent_stop.is_set():
        try:
            if screen:
                img = screen.grab()
                b64 = ScreenCapture.to_b64(img)
                socketio.emit("frame", {"image": b64})
        except Exception as e:
            socketio.emit("log", {"text": f"[Screen] Error: {e}"})
        socketio.sleep(0.5)


def _home_cursor() -> None:
    """Send cursor to top-left corner (0, 0). Single large command — ESP32 chunks internally."""
    global cursor_x, cursor_y
    hid.send({"type": "move", "dx": -10000, "dy": -10000})
    time.sleep(0.3)
    hid.send({"type": "move", "dx": -10000, "dy": -10000})
    time.sleep(0.3)
    cursor_x = 0
    cursor_y = 0


def move_cursor_to(x: int, y: int) -> None:
    """Move cursor to absolute (x, y) via home-then-move.

    Always homes first for accuracy — relative moves accumulate drift
    which can cause clicking the wrong button (e.g. PREV instead of NEXT).
    """
    global cursor_x, cursor_y

    x = max(0, min(SCREEN_WIDTH, x))
    y = max(0, min(SCREEN_HEIGHT, y))

    _home_cursor()

    target_mx = round(x * MICKEY_SCALE_X)
    target_my = round(y * MICKEY_SCALE_Y)
    hid.send({"type": "move", "dx": target_mx, "dy": target_my})
    time.sleep(0.2)

    cursor_x = x
    cursor_y = y


def execute_action(action: dict) -> bool:
    """Execute a normalized action via HID. Returns True if acked."""
    action_type = action.get("type", "unknown")

    if action_type in ("click_at", "double_click_at", "right_click_at", "move_to", "scroll_at"):
        target_x = action.get("x", SCREEN_WIDTH // 2)
        target_y = action.get("y", SCREEN_HEIGHT // 2)

        socketio.emit("log", {"text": f"[Move] Positioning cursor at ({target_x}, {target_y})…"})
        move_cursor_to(target_x, target_y)

        if action_type == "click_at":
            hid.send({"type": "click", "button": "left"})
            socketio.emit("log", {"text": "[HID] Click"})
        elif action_type == "double_click_at":
            hid.send({"type": "double_click", "button": "left"})
            socketio.emit("log", {"text": "[HID] Double-click"})
        elif action_type == "right_click_at":
            hid.send({"type": "right_click", "button": "right"})
            socketio.emit("log", {"text": "[HID] Right-click"})
        elif action_type == "scroll_at":
            scroll_dy = action.get("dy", 0)
            hid.send({"type": "scroll", "dx": 0, "dy": scroll_dy})
            socketio.emit("log", {"text": f"[HID] Scroll dy={scroll_dy}"})
        elif action_type == "move_to":
            socketio.emit("log", {"text": "[HID] Move complete"})
        return True

    elif action_type == "key":
        return hid.send(action)
    elif action_type == "type":
        return hid.send(action)
    elif action_type == "wait":
        duration = action.get("duration", 2)
        socketio.emit("log", {"text": f"[HID] Waiting {duration}s…"})
        time.sleep(duration)
        return True
    else:
        return hid.send(action)


def agent_loop() -> None:
    global current_task

    init_hardware()

    # Apply active task profile to the AI
    if active_profile:
        profile_text = load_profile(active_profile)
        claude.set_profile(profile_text)
        socketio.emit("log", {"text": f"[Config] Profile: {active_profile}"})
    else:
        claude.set_profile("")
        socketio.emit("log", {"text": "[Config] Profile: none (generic)"})

    socketio.emit("log", {"text": f"[Config] MICKEY_SCALE X={MICKEY_SCALE_X:.3f} Y={MICKEY_SCALE_Y:.3f}"})
    _home_cursor()  # start from known position

    # record task
    task_entry = {
        "task": current_task,
        "time": datetime.now().strftime("%H:%M:%S"),
        "status": "running",
        "steps": 0,
    }
    task_log.append(task_entry)
    save_task_log()
    socketio.emit("task_log", task_log)

    # inject session context
    session_ctx = build_session_context()
    task_with_context = current_task
    if session_ctx:
        task_with_context = f"{current_task}\n\n[Session context]\n{session_ctx}"

    history: list[dict] = []
    step = 0
    consecutive_screenshots = 0
    prev_was_stuck = False
    prev_stuck_context = ""
    prev_observe = ""

    while not agent_stop.is_set():
        step += 1
        task_entry["steps"] = step
        socketio.emit("log", {"text": f"\n=== Step {step} ==="})

        # streaming callback
        socketio.emit("thinking_start", {})

        def on_token(token: str) -> None:
            socketio.emit("thinking_token", {"token": token})

        # Drain any live guidance from the user
        pending_guidance: list[str] = []
        with guidance_lock:
            if guidance_queue:
                pending_guidance = guidance_queue.copy()
                guidance_queue.clear()

        # Inject guidance into history so the AI sees it
        if pending_guidance:
            guidance_text = "\n".join(f"- {g}" for g in pending_guidance)
            history.append({
                "user": (
                    f"USER GUIDANCE (follow these instructions from the human operator):\n"
                    f"{guidance_text}\n"
                    f"Acknowledge the guidance briefly in your THINK step, then act on it."
                ),
                "assistant": "Understood, I will follow the operator's guidance.",
            })

        step_start = time.time()
        try:
            img = screen.grab()
            b64 = ScreenCapture.to_b64(img)
            action = claude.decide(task_with_context, b64, history, on_token=on_token)
        except ParseError as e:
            socketio.emit("thinking_end", {})
            socketio.emit("log", {"text": f"[ParseError] {e} — retaking screenshot and retrying…"})
            continue
        except Exception as e:
            socketio.emit("thinking_end", {})
            socketio.emit("log", {"text": f"[Error] {e}"})
            task_entry["status"] = f"error: {e}"
            save_task_log()
            break

        socketio.emit("thinking_end", {})

        action_type = action.get("type", "unknown")
        socketio.emit("log", {"text": f"[Action] {action}"})
        socketio.emit("action", action)

        cost = claude.get_cost()
        socketio.emit("cost", cost)

        # Store full reasoning in history so the AI remembers WHY it did things
        reasoning = action.pop("_reasoning", str(action))
        history.append({
            "user": f"Step {step}. Screenshot attached. Decide next action.",
            "assistant": reasoning,
        })

        # --- Profile learning: detect mistakes and save lessons ---
        if active_profile:
            # Extract current OBSERVE text
            cur_observe = ""
            reasoning_lower = reasoning.lower()
            if "observe:" in reasoning_lower:
                cur_observe = reasoning_lower.split("observe:")[1][:200].strip()

            def _save_lesson(lesson: str) -> None:
                if add_lesson(active_profile, lesson):
                    socketio.emit("log", {"text": f"[Learn] Saved: {lesson}"})
                    claude.set_profile(load_profile(active_profile))

            def _describe_action(a_type: str, a: dict) -> str:
                if "click" in a_type:
                    return f"click at ({a.get('x','?')},{a.get('y','?')})"
                if a_type == "wait":
                    return f"wait {a.get('duration','')}s"
                if a_type == "key":
                    return f"key {a.get('keys','')}"
                return a_type

            # 1) Stuck→recovery: was stuck, then page changed
            if prev_was_stuck and cur_observe and prev_observe:
                cur_words = set(cur_observe.split())
                prev_words = set(prev_observe.split())
                if prev_words and cur_words:
                    overlap = len(cur_words & prev_words) / max(len(cur_words | prev_words), 1)
                    if overlap < 0.5:
                        context_short = prev_stuck_context[:80].strip()
                        _save_lesson(
                            f"Stuck on '{context_short}' — resolved by: "
                            f"{_describe_action(action_type, action)}"
                        )

            # 2) Page unchanged after action: AI tried something that didn't work
            #    If same OBSERVE 3 turns in a row, learn what's NOT working
            if len(history) >= 3 and cur_observe and prev_observe:
                cur_words = set(cur_observe.split())
                prev_words = set(prev_observe.split())
                if cur_words and prev_words:
                    same_page = (
                        len(cur_words & prev_words) / max(len(cur_words | prev_words), 1) > 0.6
                    )
                    if same_page:
                        # Check the action from 2 steps ago too
                        two_ago_obs = ""
                        if len(history) >= 3:
                            two_ago = history[-3]["assistant"].lower()
                            if "observe:" in two_ago:
                                two_ago_obs = two_ago.split("observe:")[1][:200].strip()
                        two_ago_words = set(two_ago_obs.split()) if two_ago_obs else set()
                        three_same = (
                            two_ago_words and cur_words
                            and len(cur_words & two_ago_words) / max(len(cur_words | two_ago_words), 1) > 0.6
                        )
                        if three_same:
                            # 3 turns on same page — extract what failed
                            failed_actions = []
                            for h in history[-3:-1]:
                                m = re.search(r'\{[^{}]*\}', h["assistant"])
                                if m:
                                    try:
                                        a = json.loads(m.group(0))
                                        failed_actions.append(
                                            _describe_action(a.get("type", ""), a)
                                        )
                                    except json.JSONDecodeError:
                                        pass
                            if failed_actions:
                                page_short = cur_observe[:60].strip()
                                _save_lesson(
                                    f"On '{page_short}': "
                                    f"{', '.join(failed_actions)} did NOT advance the page"
                                )

            # 3) Backward navigation: current page matches something from 5+ steps ago
            if len(history) >= 6 and cur_observe:
                cur_words = set(cur_observe.split())
                if len(cur_words) > 4:
                    for h in history[:-5]:
                        old_lower = h["assistant"].lower()
                        if "observe:" in old_lower:
                            old_obs = old_lower.split("observe:")[1][:200].strip()
                            old_words = set(old_obs.split())
                            if len(old_words) > 4:
                                overlap = len(cur_words & old_words) / min(len(cur_words), len(old_words))
                                if overlap > 0.6:
                                    _save_lesson(
                                        f"Went backward to '{cur_observe[:60]}' "
                                        f"— avoid clicking sidebar/menu for earlier sections"
                                    )
                                    break

            # Track for next iteration
            prev_was_stuck = claude.was_stuck
            prev_stuck_context = claude.stuck_context
            prev_observe = cur_observe

        if action_type == "done":
            msg = action.get("message", "")
            socketio.emit("log", {"text": f"[Agent] Done — {msg}"})
            task_entry["status"] = f"done: {msg}"
            save_task_log()
            socketio.emit("task_log", task_log)
            socketio.emit("status", {"running": False})
            return

        if action_type == "screenshot":
            consecutive_screenshots += 1
            if consecutive_screenshots >= 2:
                socketio.emit("log", {"text": "[Agent] Max screenshots — must act next."})
                history[-1]["user"] += " SYSTEM: You took 2 screenshots. You MUST act now."
            else:
                socketio.emit("log", {"text": f"[Agent] Screenshot only ({consecutive_screenshots}/2)."})
            continue

        consecutive_screenshots = 0

        acked = execute_action(action)
        if acked:
            socketio.emit("log", {"text": "[Agent] ESP32 ACK"})
        else:
            socketio.emit("log", {"text": "[Agent] WARNING: no ACK"})

        # Wait for the screen to settle after the action, but account for
        # time already spent on AI thinking + action execution
        elapsed = time.time() - step_start
        remaining = max(0, prompt_rate - elapsed)
        if remaining > 0:
            waited = 0.0
            while waited < remaining and not agent_stop.is_set():
                time.sleep(0.5)
                waited += 0.5
        else:
            # Still wait a minimum of 1s for the screen to update after a click
            time.sleep(1.0)

    if task_entry["status"] == "running":
        task_entry["status"] = "stopped"
    save_task_log()
    socketio.emit("task_log", task_log)
    socketio.emit("log", {"text": "[Agent] Stopped."})
    socketio.emit("status", {"running": False})


# --- routes -----------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# --- socket events ----------------------------------------------------------

_startup_checked = False

@socketio.on("connect")
def on_connect():
    global _startup_checked
    # Run hardware health checks once on first client connect
    if not _startup_checked:
        _startup_checked = True
        checks = startup_check()
        for c in checks:
            status = "OK" if c["ok"] else "FAIL"
            socketio.emit("log", {"text": f"[Health] [{status}] {c['component']}: {c['message']}"})
            if not c["ok"] and c["fixes"]:
                for fix in c["fixes"]:
                    socketio.emit("log", {"text": f"[Health]   -> {fix}"})

    try:
        init_hardware()
    except Exception as e:
        socketio.emit("log", {"text": f"[Init] {e}"})
    socketio.emit("settings", {"prompt_rate": prompt_rate})
    socketio.emit("status", {"running": agent_thread is not None and agent_thread.is_alive()})
    socketio.emit("task_log", task_log)
    socketio.emit("session_notes", session_notes)
    socketio.emit("profile", {"active": active_profile, "profiles": list_profiles()})
    socketio.start_background_task(stream_screen)


@socketio.on("start_task")
def on_start_task(data):
    global agent_thread, current_task
    task = data.get("task", "").strip()
    if not task:
        socketio.emit("log", {"text": "[Error] No task provided."})
        return

    # If already running, stop the current task first and wait for it to finish
    if agent_thread and agent_thread.is_alive():
        socketio.emit("log", {"text": "[Agent] Stopping current task for new one…"})
        agent_stop.set()
        agent_thread.join(timeout=5)

    current_task = task
    claude.reset()
    agent_stop.clear()
    with guidance_lock:
        guidance_queue.clear()

    socketio.emit("log", {"text": f"\n[Agent] Starting task: {task}"})
    socketio.emit("status", {"running": True})

    agent_thread = threading.Thread(target=agent_loop, daemon=True)
    agent_thread.start()


@socketio.on("stop_task")
def on_stop_task():
    agent_stop.set()
    socketio.emit("log", {"text": "[Agent] Stop requested."})
    socketio.emit("status", {"running": False})


@socketio.on("send_guidance")
def on_send_guidance(data):
    """Live guidance from user while agent is running."""
    msg = data.get("message", "").strip()
    if not msg:
        return
    with guidance_lock:
        guidance_queue.append(msg)
    socketio.emit("log", {"text": f"[You] {msg}"})
    # Also save as a lesson if a profile is active
    if active_profile:
        add_lesson(active_profile, f"User guidance: {msg}")
        socketio.emit("log", {"text": f"[Learn] Saved guidance as lesson"})


@socketio.on("set_prompt_rate")
def on_set_prompt_rate(data):
    global prompt_rate
    rate = float(data.get("rate", 5))
    prompt_rate = max(1.0, rate)
    socketio.emit("log", {"text": f"[Settings] Prompt rate set to {prompt_rate}s"})
    socketio.emit("settings", {"prompt_rate": prompt_rate})


@socketio.on("add_note")
def on_add_note(data):
    note = data.get("note", "").strip()
    if note:
        session_notes.append(note)
        save_notes()
        socketio.emit("session_notes", session_notes)
        socketio.emit("log", {"text": f"[Memory] Saved: {note}"})


@socketio.on("clear_notes")
def on_clear_notes():
    session_notes.clear()
    save_notes()
    socketio.emit("session_notes", session_notes)
    socketio.emit("log", {"text": "[Memory] Session notes cleared."})


@socketio.on("clear_task_log")
def on_clear_task_log():
    task_log.clear()
    save_task_log()
    socketio.emit("task_log", task_log)
    socketio.emit("log", {"text": "[History] Task log cleared."})


@socketio.on("calibrate")
def on_calibrate():
    """Manually trigger mouse calibration."""
    global calibrated
    calibrated = False
    try:
        init_hardware()
        calibrate_mouse()
    except CalibrationError:
        pass  # already reported via log + calibration_error event
    except Exception as e:
        socketio.emit("log", {"text": f"[Calibration] Unexpected error: {e}"})


@socketio.on("adjust_scale")
def on_adjust_scale(data):
    """Manually adjust MICKEY_SCALE_X/Y — 2% steps for fine tuning."""
    global MICKEY_SCALE_X, MICKEY_SCALE_Y
    direction = data.get("direction", "")
    if direction == "up":
        MICKEY_SCALE_X *= 1.02
        MICKEY_SCALE_Y *= 1.02
    elif direction == "down":
        MICKEY_SCALE_X /= 1.02
        MICKEY_SCALE_Y /= 1.02
    socketio.emit("log", {"text": f"[Config] MICKEY_SCALE X={MICKEY_SCALE_X:.4f} Y={MICKEY_SCALE_Y:.4f}"})


@socketio.on("click_test")
def on_click_test(data):
    """Move cursor to a target and click — lets the user visually verify accuracy.

    Send {"x": 640, "y": 360} to test clicking screen center.
    The cursor will move there and click, so the user can see on the
    live screen feed exactly where the cursor landed.
    """
    try:
        init_hardware()
        x = int(data.get("x", SCREEN_WIDTH // 2))
        y = int(data.get("y", SCREEN_HEIGHT // 2))
        socketio.emit("log", {"text": f"[ClickTest] Moving to ({x}, {y}) with scale X={MICKEY_SCALE_X:.4f} Y={MICKEY_SCALE_Y:.4f}"})
        move_cursor_to(x, y)
        hid.send({"type": "click", "button": "left"})
        socketio.emit("log", {"text": f"[ClickTest] Clicked at ({x}, {y}). Check the live screen to verify accuracy."})
    except Exception as e:
        socketio.emit("log", {"text": f"[ClickTest] Error: {e}"})


@socketio.on("send_hid")
def on_send_hid(data):
    try:
        init_hardware()
        acked = hid.send(data)
        socketio.emit("log", {"text": f"[Manual] {data} → {'ACK' if acked else 'NO ACK'}"})
    except Exception as e:
        socketio.emit("log", {"text": f"[Manual] Error: {e}"})


@socketio.on("set_profile")
def on_set_profile(data):
    global active_profile
    name = data.get("profile", "")
    if name and name not in list_profiles():
        socketio.emit("log", {"text": f"[Profile] Unknown profile: {name}"})
        return
    active_profile = name
    label = name if name else "none (generic)"
    socketio.emit("log", {"text": f"[Profile] Set to: {label}"})
    socketio.emit("profile", {"active": active_profile, "profiles": list_profiles()})


@socketio.on("get_profiles")
def on_get_profiles():
    socketio.emit("profile", {"active": active_profile, "profiles": list_profiles()})


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  STARTUP HARDWARE CHECK")
    print("=" * 60)
    checks = startup_check()
    all_ok = True
    for c in checks:
        status = "OK" if c["ok"] else "FAIL"
        print(f"\n  [{status}] {c['component']}: {c['message']}")
        if not c["ok"]:
            all_ok = False
            if c["fixes"]:
                print("  Possible fixes:")
                for fix in c["fixes"]:
                    print(f"    - {fix}")
    print("\n" + "=" * 60)
    if all_ok:
        print("  All checks passed — starting server")
    else:
        print("  Some checks FAILED — starting server anyway (fix issues above)")
    print("=" * 60 + "\n")

    socketio.run(app, host="0.0.0.0", port=5000, debug=False)
