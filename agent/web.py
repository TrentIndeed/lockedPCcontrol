"""Web interface — live screen view + chat to control the agent."""

import json
import os
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

screen: ScreenCapture | None = None
hid: HIDPublisher | None = None
claude: ClaudeClient | None = None

prompt_rate: float = _CFG_PROMPT_RATE
current_task: str = ""
max_steps: int = MAX_STEPS

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


calibrated = False

# Calibration constants
_CAL_MICKEYS_SHORT = 200   # first probe distance
_CAL_MICKEYS_LONG = 400    # second probe distance (2x short)
_ACCEL_RATIO_TOL = 0.15    # max deviation from 2.0 before flagging acceleration
_CAL_ACCURACY_TOL = 40     # max pixel error for calibration to pass


class CalibrationError(Exception):
    """Raised when calibration detects a problem."""
    pass


def _ask_cursor_position(screenshot_b64: str) -> tuple[int, int]:
    """Ask the AI to locate the mouse cursor in a screenshot.

    Returns (x, y) pixel coordinates.
    Raises CalibrationError if the AI can't find the cursor.
    """
    result = claude.decide(
        "Find the mouse cursor in this screenshot. Report its EXACT pixel "
        "coordinates. Reply ONLY with JSON: {\"type\":\"done\",\"x\":<int>,\"y\":<int>}",
        screenshot_b64,
        [],
    )
    x = result.get("x")
    y = result.get("y")
    if x is None or y is None:
        raise CalibrationError(f"AI could not locate cursor: {result}")
    return int(x), int(y)


def calibrate_mouse() -> None:
    """Detect mouse acceleration and verify/calibrate MICKEY_SCALE.

    Steps:
    1. Home cursor, move SHORT mickeys right, ask AI for cursor position.
    2. Home cursor, move LONG mickeys right, ask AI for cursor position.
    3. Compare pixel-per-mickey ratio at both distances.
       If non-linear → mouse acceleration is ON → raise error.
    4. Derive scale from measurements.
    5. Move to a known target, verify the cursor lands within tolerance.
    """
    global calibrated, MICKEY_SCALE_X, MICKEY_SCALE_Y

    if calibrated:
        socketio.emit("log", {"text": "[Calibration] Already calibrated. Skipping."})
        return

    init_hardware()
    socketio.emit("log", {"text": "[Calibration] Starting acceleration detection..."})

    # --- Probe 1: short distance -------------------------------------------
    _home_cursor()
    time.sleep(0.5)
    hid.send({"type": "move", "dx": _CAL_MICKEYS_SHORT, "dy": 0})
    time.sleep(0.5)

    img1 = screen.grab()
    b64_1 = ScreenCapture.to_b64(img1)
    try:
        x1, y1 = _ask_cursor_position(b64_1)
    except (CalibrationError, Exception) as e:
        socketio.emit("log", {"text": f"[Calibration] WARN: Could not find cursor after short move: {e}"})
        socketio.emit("log", {"text": "[Calibration] Falling back to config scale values."})
        calibrated = True
        return

    ppm_short = x1 / _CAL_MICKEYS_SHORT if _CAL_MICKEYS_SHORT else 0
    socketio.emit("log", {"text": f"[Calibration] Short move: {_CAL_MICKEYS_SHORT} mickeys -> cursor at ({x1}, {y1}), ppm={ppm_short:.3f}"})

    # --- Probe 2: long distance --------------------------------------------
    _home_cursor()
    time.sleep(0.5)
    hid.send({"type": "move", "dx": _CAL_MICKEYS_LONG, "dy": 0})
    time.sleep(0.5)

    img2 = screen.grab()
    b64_2 = ScreenCapture.to_b64(img2)
    try:
        x2, y2 = _ask_cursor_position(b64_2)
    except (CalibrationError, Exception) as e:
        socketio.emit("log", {"text": f"[Calibration] WARN: Could not find cursor after long move: {e}"})
        socketio.emit("log", {"text": "[Calibration] Falling back to config scale values."})
        calibrated = True
        return

    ppm_long = x2 / _CAL_MICKEYS_LONG if _CAL_MICKEYS_LONG else 0
    socketio.emit("log", {"text": f"[Calibration] Long move: {_CAL_MICKEYS_LONG} mickeys -> cursor at ({x2}, {y2}), ppm={ppm_long:.3f}"})

    # --- Acceleration check ------------------------------------------------
    if ppm_short > 0:
        actual_ratio = x2 / x1
        expected_ratio = _CAL_MICKEYS_LONG / _CAL_MICKEYS_SHORT  # should be 2.0
        deviation = abs(actual_ratio - expected_ratio) / expected_ratio

        socketio.emit("log", {
            "text": f"[Calibration] Distance ratio: {actual_ratio:.2f} (expected {expected_ratio:.1f}, deviation {deviation:.1%})"
        })

        if deviation > _ACCEL_RATIO_TOL:
            msg = (
                f"MOUSE ACCELERATION DETECTED! Distance ratio is {actual_ratio:.2f} "
                f"(expected {expected_ratio:.1f}, {deviation:.0%} off). "
                f"Disable 'Enhance pointer precision' in the locked PC's mouse settings "
                f"(Control Panel -> Mouse -> Pointer Options -> uncheck 'Enhance pointer precision'). "
                f"Agent cannot aim accurately with acceleration ON."
            )
            socketio.emit("log", {"text": f"[Calibration] ERROR: {msg}"})
            socketio.emit("calibration_error", {"error": "acceleration", "message": msg})
            raise CalibrationError(msg)
    else:
        socketio.emit("log", {"text": "[Calibration] WARN: Short probe returned x=0, cannot check acceleration."})

    # --- Derive scale ------------------------------------------------------
    # Average ppm from both probes (should be nearly identical if no accel)
    avg_ppm = (ppm_short + ppm_long) / 2
    if avg_ppm > 0:
        new_scale = 1.0 / avg_ppm
        socketio.emit("log", {
            "text": f"[Calibration] Measured ppm={avg_ppm:.3f}, derived scale={new_scale:.4f} "
                    f"(was X={MICKEY_SCALE_X:.4f} Y={MICKEY_SCALE_Y:.4f})"
        })
        MICKEY_SCALE_X = new_scale
        MICKEY_SCALE_Y = new_scale

    # --- Verification: move to known target and check ----------------------
    target_x, target_y = SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2
    socketio.emit("log", {"text": f"[Calibration] Verifying: moving to ({target_x}, {target_y})..."})
    move_cursor_to(target_x, target_y)
    time.sleep(0.5)

    img3 = screen.grab()
    b64_3 = ScreenCapture.to_b64(img3)
    try:
        actual_x, actual_y = _ask_cursor_position(b64_3)
    except (CalibrationError, Exception) as e:
        socketio.emit("log", {"text": f"[Calibration] WARN: Could not verify cursor position: {e}"})
        socketio.emit("log", {"text": f"[Calibration] Done (unverified). Scale X={MICKEY_SCALE_X:.4f} Y={MICKEY_SCALE_Y:.4f}"})
        calibrated = True
        return

    err_x = abs(actual_x - target_x)
    err_y = abs(actual_y - target_y)
    err_total = (err_x ** 2 + err_y ** 2) ** 0.5

    socketio.emit("log", {
        "text": f"[Calibration] Verification: target=({target_x},{target_y}) "
                f"actual=({actual_x},{actual_y}) error={err_total:.0f}px "
                f"(dx={err_x}, dy={err_y})"
    })

    if err_total > _CAL_ACCURACY_TOL:
        msg = (
            f"Calibration accuracy check FAILED. Cursor landed {err_total:.0f}px from target "
            f"(tolerance is {_CAL_ACCURACY_TOL}px). Target=({target_x},{target_y}), "
            f"Actual=({actual_x},{actual_y}). Try adjusting mickey_scale_x/y in agent.yaml "
            f"or re-run calibration."
        )
        socketio.emit("log", {"text": f"[Calibration] ERROR: {msg}"})
        socketio.emit("calibration_error", {"error": "accuracy", "message": msg})
        raise CalibrationError(msg)

    socketio.emit("log", {
        "text": f"[Calibration] PASS. Scale X={MICKEY_SCALE_X:.4f} Y={MICKEY_SCALE_Y:.4f}, "
                f"accuracy={err_total:.0f}px"
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

    Sends multiple fixed-size chunks (CHUNK_SIZE mickeys each) to keep
    Windows acceleration consistent per chunk, making total movement linear.
    """
    global cursor_x, cursor_y

    x = max(0, min(SCREEN_WIDTH, x))
    y = max(0, min(SCREEN_HEIGHT, y))

    _home_cursor()

    target_mx = int(x * MICKEY_SCALE_X)
    target_my = int(y * MICKEY_SCALE_Y)

    # Send as single command — ESP32 chunks at ±127 internally with 5ms delay.
    # The consistent chunk speed means acceleration is a constant multiplier.
    hid.send({"type": "move", "dx": target_mx, "dy": target_my})
    time.sleep(0.2)
    cursor_x = x
    cursor_y = y


_EDGE_MARGIN = 50   # pixels from edge considered "near edge"
_EDGE_NUDGE = 8     # pixels to nudge inward for edge clicks


def execute_action(action: dict) -> bool:
    """Execute a normalized action via HID. Returns True if acked."""
    action_type = action.get("type", "unknown")

    if action_type in ("click_at", "double_click_at", "right_click_at", "move_to", "scroll_at"):
        target_x = action.get("x", SCREEN_WIDTH // 2)
        target_y = action.get("y", SCREEN_HEIGHT // 2)

        # Nudge clicks near screen edges inward — cursor positioning is less
        # accurate at extremes due to boundary clamping and rounding errors
        nudged = False
        if target_x > SCREEN_WIDTH - _EDGE_MARGIN:
            target_x -= _EDGE_NUDGE
            nudged = True
        elif target_x < _EDGE_MARGIN:
            target_x += _EDGE_NUDGE
            nudged = True
        if target_y > SCREEN_HEIGHT - _EDGE_MARGIN:
            target_y -= _EDGE_NUDGE
            nudged = True
        elif target_y < _EDGE_MARGIN:
            target_y += _EDGE_NUDGE
            nudged = True

        if nudged:
            socketio.emit("log", {"text": f"[Move] Edge nudge -> ({target_x}, {target_y})"})

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

    # Run calibration check on first task (detects acceleration + verifies scale)
    try:
        calibrate_mouse()
    except CalibrationError as e:
        socketio.emit("log", {"text": f"[Agent] Cannot start — calibration failed: {e}"})
        socketio.emit("status", {"running": False})
        return

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

    while not agent_stop.is_set() and step < max_steps:
        step += 1
        task_entry["steps"] = step
        socketio.emit("log", {"text": f"\n=== Step {step} ==="})

        # streaming callback
        socketio.emit("thinking_start", {})

        def on_token(token: str) -> None:
            socketio.emit("thinking_token", {"token": token})

        step_start = time.time()
        try:
            img = screen.grab()
            b64 = ScreenCapture.to_b64(img)
            action = claude.decide(task_with_context, b64, history, on_token=on_token)
        except ParseError as e:
            socketio.emit("thinking_end", {})
            socketio.emit("log", {"text": f"[ParseError] {e} — retaking screenshot and retrying…"})
            # Don't crash — the AI likely ran out of tokens. Retry once.
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

@socketio.on("connect")
def on_connect():
    try:
        init_hardware()
    except Exception as e:
        socketio.emit("log", {"text": f"[Init] {e}"})
    socketio.emit("settings", {"prompt_rate": prompt_rate})
    socketio.emit("status", {"running": agent_thread is not None and agent_thread.is_alive()})
    socketio.emit("task_log", task_log)
    socketio.emit("session_notes", session_notes)
    socketio.start_background_task(stream_screen)


@socketio.on("start_task")
def on_start_task(data):
    global agent_thread, current_task
    task = data.get("task", "").strip()
    if not task:
        socketio.emit("log", {"text": "[Error] No task provided."})
        return

    if agent_thread and agent_thread.is_alive():
        socketio.emit("log", {"text": "[Agent] Already running. Stop first."})
        return

    current_task = task
    claude.reset()  # clear previous conversation state
    agent_stop.clear()

    socketio.emit("log", {"text": f"[Agent] Starting task: {task}"})
    socketio.emit("status", {"running": True})

    agent_thread = threading.Thread(target=agent_loop, daemon=True)
    agent_thread.start()


@socketio.on("stop_task")
def on_stop_task():
    agent_stop.set()
    socketio.emit("log", {"text": "[Agent] Stop requested."})
    socketio.emit("status", {"running": False})


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
    """Manually adjust MICKEY_SCALE_X/Y up or down."""
    global MICKEY_SCALE_X, MICKEY_SCALE_Y
    direction = data.get("direction", "")
    if direction == "up":
        MICKEY_SCALE_X *= 1.1
        MICKEY_SCALE_Y *= 1.1
    elif direction == "down":
        MICKEY_SCALE_X /= 1.1
        MICKEY_SCALE_Y /= 1.1
    socketio.emit("log", {"text": f"[Config] MICKEY_SCALE X={MICKEY_SCALE_X:.3f} Y={MICKEY_SCALE_Y:.3f}"})


@socketio.on("send_hid")
def on_send_hid(data):
    try:
        init_hardware()
        acked = hid.send(data)
        socketio.emit("log", {"text": f"[Manual] {data} → {'ACK' if acked else 'NO ACK'}"})
    except Exception as e:
        socketio.emit("log", {"text": f"[Manual] Error: {e}"})


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=False)
