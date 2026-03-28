# Locked PC Control

AI agent that autonomously controls a remote Windows PC through physical hardware -- no software installation needed on the target machine.

## How It Works

```
Your PC (agent)                         Locked PC (target)
+-----------------+                     +------------------+
| Python agent    |   USB serial        | Any Windows PC   |
| + web UI        |-----(COM port)----->| ESP32-S3 as USB  |
| + AI model      |                     | keyboard & mouse |
+-----------------+                     +------------------+
       ^                                       |
       | USB capture card                      | HDMI out
       +---------------------------------------+
         (HDMI splitter if monitor needed)
```

1. **HDMI capture card** reads the target PC's screen as a webcam feed
2. **ESP32-S3** plugs into the target PC's USB port and acts as a physical keyboard + mouse
3. **AI model** sees the screen, decides what to click/type, sends commands via serial to the ESP32
4. **Web UI** at `localhost:5000` shows the live screen feed and lets you issue tasks

## Hardware Required

| Part | Purpose | Notes |
|------|---------|-------|
| ESP32-S3-WROOM-1 (N16R8) | USB HID device (mouse + keyboard) | Must be S3 variant for native USB OTG |
| USB HDMI capture card | Screen reading | Shows up as a webcam; ~$10 on Amazon |
| HDMI splitter (optional) | Keep a monitor connected | Passive dongle, not powered |
| USB cable | Serial connection to ESP32 | Agent PC to ESP32's UART/debug port |
| HDMI cable | Target PC to capture card | Standard HDMI |

## Setup

### 1. Flash the ESP32-S3

Requires [PlatformIO](https://platformio.org/).

```bash
cd esp32
pio run --target upload
```

The firmware makes the ESP32 appear as a USB HID mouse + keyboard to the target PC. It receives JSON commands over serial from the agent and translates them to physical input.

Connect the ESP32's **USB OTG port** to the target PC (this is the HID side), and the **UART/debug port** to your agent PC (this is the serial command side).

### 2. Connect the HDMI capture card

Plug the target PC's HDMI output into the capture card (use a splitter if you also want a monitor). Plug the capture card's USB into your agent PC. It should appear as a webcam device.

### 3. Install Python dependencies

```bash
cd agent
pip install -r requirements.txt
```

Requires Python 3.11+.

### 4. Configure

Edit `agent/agent.yaml`:

```yaml
# AI model endpoint (any OpenAI-compatible API)
api_base_url: "http://your-api-endpoint/v1"
model: "your-model-name"

# Hardware -- adjust these for your setup
serial_port: "COM5"          # ESP32 serial port (check Device Manager)
serial_baud: 115200
capture_device_id: 1         # webcam device index (0 = built-in cam, try 1, 2, 3)

# Screen resolution sent to AI
screen_width: 1280
screen_height: 720

# Mouse calibration -- mickeys per pixel
# Start with 0.79, then fine-tune with the Test Click button in the UI
mickey_scale_x: 0.79
mickey_scale_y: 0.79
```

To override without editing the main file, create `agent/agent.local.yaml` with just the values you want to change. Environment variables also work (e.g. `SERIAL_PORT=COM3`).

### 5. Run

```bash
cd agent
python web.py
```

Open `http://localhost:5000` in your browser. On startup, the system runs hardware health checks and reports any issues with suggested fixes.

## Usage

1. Type a task in the chat box (e.g., "Open Chrome and go to google.com")
2. Click **Send** -- the agent takes a screenshot, sends it to the AI, and executes the action
3. Watch the live screen feed and log to monitor progress
4. Click **Stop** to halt the agent at any time

### Task Profiles

The agent ships with a generic system prompt that works for any task. For specialized use cases, you can load a **profile** that adds extra instructions:

- Select a profile from the **Profile** dropdown in the header
- **none (generic)** -- works for any task: Excel, web browsing, desktop apps, etc.
- **elearning** -- specialized rules for completing online training courses

Create your own profiles by adding a `.txt` file to `agent/profiles/`. The file contents are appended to the system prompt when that profile is active.

Example: `agent/profiles/excel.txt`
```
EXCEL-SPECIFIC RULES:
- Use keyboard shortcuts when possible (Ctrl+C, Ctrl+V, Ctrl+S, etc.)
- Click on a cell before typing to ensure it is selected.
- After entering data in a cell, press Enter to confirm before moving on.
- Use the formula bar for complex formulas rather than typing directly in cells.
```

### Calibration

Mouse accuracy depends on the `mickey_scale_x` / `mickey_scale_y` values matching the target PC's pointer speed setting.

1. Click **Test Click** in the UI -- this clicks the center of the screen
2. If the click lands off-center, use the **Scale +/-** buttons (2% per step) to adjust
3. Scale up if the cursor undershoots, scale down if it overshoots
4. Click **Calibrate** for automatic scale detection (goes to desktop, uses right-click context menu detection)

**Important:** Disable "Enhance pointer precision" (mouse acceleration) on the target PC:
`Control Panel > Mouse > Pointer Options > uncheck "Enhance pointer precision"`

### Session Notes

Use the **Session Notes** panel to leave persistent notes for the agent. Notes are injected as context with every task. Useful for things like:
- "The password for this site is hunter2"
- "Skip the intro video, go straight to lesson 3"

## Project Structure

```
lockedPCcontrol/
├── agent/
│   ├── web.py               # Flask + SocketIO server (main entry point)
│   ├── claude_client.py      # AI client, prompt construction, stuck detection
│   ├── hid_publisher.py      # Serial communication with ESP32
│   ├── screen.py             # HDMI capture card screen reading
│   ├── config.py             # YAML config loader
│   ├── agent.yaml            # Configuration file
│   ├── system_prompt.txt     # AI system prompt (generic, editable)
│   ├── profiles/             # Task-specific prompt profiles
│   │   └── elearning.txt     # E-learning/training course profile
│   ├── templates/
│   │   └── index.html        # Web UI
│   └── requirements.txt
└── esp32/
    ├── src/
    │   └── main.cpp          # ESP32-S3 firmware (USB HID + serial)
    └── platformio.ini
```

## HID Command Schema

The ESP32 accepts JSON commands over serial:

```json
{"type": "move",         "dx": 100, "dy": -50}
{"type": "click",        "button": "left"}
{"type": "right_click",  "button": "right"}
{"type": "double_click", "button": "left"}
{"type": "scroll",       "dx": 0, "dy": -3}
{"type": "key",          "keys": ["ctrl", "c"]}
{"type": "type",         "text": "hello world"}
```

The ESP32 responds with `{"status":"ok"}` after each command. Large mouse moves are chunked internally at 127 mickeys per HID report with 5ms delay.

## AI Actions

The AI model can output these actions (one per turn):

| Action | Description |
|--------|-------------|
| `click_at(x, y)` | Move cursor to position and left-click |
| `double_click_at(x, y)` | Move cursor and double-click |
| `right_click_at(x, y)` | Move cursor and right-click |
| `scroll_at(x, y, dy)` | Move cursor and scroll (negative=up, positive=down) |
| `key(keys)` | Press key combination (e.g., `["ctrl", "s"]`) |
| `type(text)` | Type text string |
| `screenshot` | Re-check screen without acting |
| `wait(duration)` | Wait N seconds before next screenshot |
| `done(message)` | Task complete |

## Troubleshooting

### ESP32 not detected
- Check USB cable (some are charge-only)
- Install CP210x or CH340 drivers
- Check Device Manager for COM port number
- Update `serial_port` in `agent.yaml`

### Capture card shows black / no signal
- Make sure the target PC is on and not sleeping
- Check HDMI cable at both ends
- Try `capture_device_id: 0`, `1`, `2` etc.
- Test with OBS or Camera app first

### Mouse clicks miss their target
- Run Test Click and adjust Scale +/- until clicks land accurately
- Disable mouse acceleration on the target PC
- Try the Calibrate button (minimizes to desktop first)

### Agent gets stuck in a loop
- The system auto-detects loops and injects warnings after 2 repeated actions
- For training courses, select the **elearning** profile
- Add session notes with hints (e.g., "click the numbered circles in order")

## Configuration Reference

| Key | Default | Description |
|-----|---------|-------------|
| `api_base_url` | — | OpenAI-compatible API endpoint |
| `model` | — | Model name |
| `max_output_tokens` | `2048` | Max tokens per AI response |
| `max_steps` | `1000` | Max actions per task |
| `serial_port` | `COM5` | ESP32 serial port |
| `serial_baud` | `115200` | Serial baud rate |
| `capture_device_id` | `1` | Webcam device index |
| `screen_width` | `1280` | Screenshot width sent to AI |
| `screen_height` | `720` | Screenshot height sent to AI |
| `mickey_scale_x` | `0.79` | Mickeys per pixel (horizontal) |
| `mickey_scale_y` | `0.79` | Mickeys per pixel (vertical) |
| `prompt_rate` | `5.0` | Min seconds between AI prompts |
