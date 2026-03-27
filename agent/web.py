"""Web interface — live screen view + chat to control the agent."""

import threading
import time

from flask import Flask, render_template
from flask_socketio import SocketIO

from config import (
    API_BASE_URL,
    CAPTURE_DEVICE_ID,
    MODEL,
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

# --- shared state -----------------------------------------------------------
agent_lock = threading.Lock()
agent_thread: threading.Thread | None = None
agent_stop = threading.Event()

screen: ScreenCapture | None = None
hid: HIDPublisher | None = None
claude: ClaudeClient | None = None

prompt_rate: float = 10.0  # seconds between steps
current_task: str = ""
history: list[dict] = []
max_steps: int = 200


def init_hardware() -> None:
    global screen, hid, claude
    if screen is None:
        screen = ScreenCapture(CAPTURE_DEVICE_ID, SCREEN_WIDTH, SCREEN_HEIGHT)
    if hid is None:
        hid = HIDPublisher(SERIAL_PORT, SERIAL_BAUD)
        hid.connect()
    if claude is None:
        claude = ClaudeClient(API_BASE_URL, MODEL, SCREEN_WIDTH, SCREEN_HEIGHT)


def stream_screen() -> None:
    """Background thread that pushes screen frames to the browser."""
    while not agent_stop.is_set():
        try:
            if screen:
                img = screen.grab()
                b64 = ScreenCapture.to_b64(img)
                socketio.emit("frame", {"image": b64})
        except Exception as e:
            socketio.emit("log", {"text": f"[Screen] Error: {e}"})
        socketio.sleep(0.5)  # ~2 fps for the live view


def agent_loop() -> None:
    """Runs the agent task loop in a background thread."""
    global current_task, history

    init_hardware()

    # home mouse
    socketio.emit("log", {"text": "[Agent] Homing mouse to top-left…"})
    hid.send({"type": "move", "dx": -5000, "dy": -5000})
    time.sleep(0.3)

    step = 0
    while not agent_stop.is_set() and step < max_steps:
        step += 1
        socketio.emit("log", {"text": f"\n=== Step {step} ==="})

        try:
            img = screen.grab()
            b64 = ScreenCapture.to_b64(img)
            action = claude.decide(current_task, b64, history)
        except ParseError as e:
            socketio.emit("log", {"text": f"[Error] {e}"})
            break
        except Exception as e:
            socketio.emit("log", {"text": f"[Error] {e}"})
            break

        action_type = action.get("type", "unknown")
        socketio.emit("log", {"text": f"[Claude] {action}"})
        socketio.emit("action", action)

        # cost
        cost = claude.get_cost()
        socketio.emit("cost", cost)

        history.append(
            {
                "user": f"Task: {current_task} (step {step})",
                "assistant": str(action),
            }
        )

        if action_type == "done":
            socketio.emit("log", {"text": f"[Agent] Done — {action.get('message', '')}"})
            socketio.emit("status", {"running": False})
            return

        if action_type == "screenshot":
            socketio.emit("log", {"text": "[Agent] Screenshot only, no HID."})
            continue

        acked = hid.send(action)
        if acked:
            socketio.emit("log", {"text": "[Agent] ESP32 ACK"})
        else:
            socketio.emit("log", {"text": "[Agent] WARNING: no ACK"})

        # wait prompt_rate seconds, but check stop flag every 0.5s
        waited = 0.0
        while waited < prompt_rate and not agent_stop.is_set():
            time.sleep(0.5)
            waited += 0.5

    socketio.emit("log", {"text": "[Agent] Stopped."})
    socketio.emit("status", {"running": False})


# --- routes -----------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# --- socket events ----------------------------------------------------------

@socketio.on("connect")
def on_connect():
    global screen
    try:
        init_hardware()
    except Exception as e:
        socketio.emit("log", {"text": f"[Init] {e}"})
    socketio.emit("settings", {"prompt_rate": prompt_rate})
    socketio.emit("status", {"running": agent_thread is not None and agent_thread.is_alive()})
    # start screen stream
    socketio.start_background_task(stream_screen)


@socketio.on("start_task")
def on_start_task(data):
    global agent_thread, current_task, history
    task = data.get("task", "").strip()
    if not task:
        socketio.emit("log", {"text": "[Error] No task provided."})
        return

    if agent_thread and agent_thread.is_alive():
        socketio.emit("log", {"text": "[Agent] Already running. Stop first."})
        return

    current_task = task
    history = []
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
    rate = float(data.get("rate", 10))
    prompt_rate = max(1.0, rate)
    socketio.emit("log", {"text": f"[Settings] Prompt rate set to {prompt_rate}s"})
    socketio.emit("settings", {"prompt_rate": prompt_rate})


@socketio.on("send_hid")
def on_send_hid(data):
    """Manual HID command from the UI."""
    try:
        init_hardware()
        acked = hid.send(data)
        socketio.emit("log", {"text": f"[Manual] {data} → {'ACK' if acked else 'NO ACK'}"})
    except Exception as e:
        socketio.emit("log", {"text": f"[Manual] Error: {e}"})


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=False)
