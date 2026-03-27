# Project: ESP32-S3 + HDMI Capture AI Agent

## Overview
Build a Python-based AI agent that controls a remote locked PC using only
physical peripherals. The locked PC has no software installed by this
project. Control is achieved by:
- Reading the locked PC's screen via an HDMI duplicator → USB capture card
- Sending mouse and keyboard input via an ESP32-S3 acting as a USB HID device
- Using Claude claude-sonnet-4-6 as the AI brain to decide what actions to take
- Communicating commands from the agent to the ESP32 over MQTT (TLS, port 8883)
  across different networks (ESP32 uses a phone hotspot or guest WiFi)

## Hardware
- HDMI duplicator dongle (passive splitter, no drivers)
- USB capture card (appears as a webcam to the agent machine)
- ESP32-S3-WROOM-1 N16R8 connected via USB OTG port to locked PC
- Agent machine (your own PC) running Python

## Repository Structure
```
esp32-agent/
├── CLAUDE_INSTRUCTIONS.md
├── agent/
│   ├── main.py              # entry point, arg parsing, task loop
│   ├── screen.py            # capture card screen reading via OpenCV
│   ├── claude_client.py     # Anthropic API calls, prompt construction
│   ├── hid_publisher.py     # MQTT publisher, sends HID commands to ESP32
│   ├── config.py            # all config: API keys, MQTT broker, topics
│   └── requirements.txt
├── esp32/
│   ├── firmware/
│   │   └── main.cpp         # Arduino sketch: USB HID + MQTT subscriber
│   └── platformio.ini       # PlatformIO config for ESP32-S3
└── tests/
    ├── test_screen.py        # verify capture card reads frames correctly
    ├── test_hid.py           # send test HID commands, verify ESP32 responds
    └── test_agent_loop.py    # mock Claude responses, verify action execution
```

## Agent — Python side (agent/)

### config.py
All secrets and settings in one place. Load from environment variables,
fall back to defaults for non-sensitive values. No hardcoded secrets.
```
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
MQTT_BROKER         = os.environ["MQTT_BROKER"]        # e.g. xxx.hivemq.cloud
MQTT_PORT           = 8883                              # TLS
MQTT_TOPIC_CMD      = "agent/hid/cmd"
MQTT_TOPIC_ACK      = "agent/hid/ack"
MQTT_USERNAME       = os.environ["MQTT_USERNAME"]
MQTT_PASSWORD       = os.environ["MQTT_PASSWORD"]
CAPTURE_DEVICE_ID   = int(os.environ.get("CAPTURE_DEVICE_ID", "0"))
SCREEN_WIDTH        = 1280
SCREEN_HEIGHT       = 720
MAX_STEPS           = 50
MODEL               = "claude-sonnet-4-6"
```

### screen.py
Class `ScreenCapture`:
- `__init__(device_id)` — opens OpenCV VideoCapture
- `grab() -> PIL.Image` — captures one frame, converts BGR→RGB,
  resizes to SCREEN_WIDTH×SCREEN_HEIGHT
- `to_b64(image) -> str` — encodes PIL image as base64 PNG string
- `release()` — closes capture device
- Raises `ScreenCaptureError` if device fails to open or frame read fails

### hid_publisher.py
Class `HIDPublisher`:
- `__init__(broker, port, username, password, cmd_topic, ack_topic)`
- `connect()` — connects with TLS, sets up on_connect and on_message callbacks
- `send(action: dict) -> bool` — publishes JSON action to cmd_topic,
  waits up to 2s for ACK on ack_topic, returns True if acknowledged
- `disconnect()`
- Actions are plain dicts matching the ESP32 schema (see below)

### claude_client.py
Class `ClaudeClient`:
- `__init__(api_key, model, screen_w, screen_h)`
- `decide(task: str, screenshot_b64: str, history: list) -> dict`
  — builds message with system prompt + image + task text,
    calls claude-sonnet-4-6, parses and returns action dict
  — history is a rolling window of last 10 exchanges (text only, no images)
  — system prompt tells Claude: screen is SCREEN_WIDTH×SCREEN_HEIGHT,
    respond with raw JSON only, one action per turn, available action types
- `_parse_response(text: str) -> dict` — strips any accidental markdown
  fences, parses JSON, validates required keys, raises `ParseError` on failure

### main.py
- CLI: `python main.py --task "open notepad and type hello world"`
- Optional flags: `--max-steps N`, `--device-id N`, `--dry-run` (no HID sends)
- Instantiates ScreenCapture, HIDPublisher, ClaudeClient
- Runs agent loop:
  1. Grab screenshot
  2. Call claude_client.decide()
  3. If action type is "done" → print message, break
  4. If action type is "screenshot" → continue without sending HID
  5. Otherwise → hid_publisher.send(action)
  6. Sleep 1s for screen to update
  7. Repeat up to MAX_STEPS
- Ctrl+C exits cleanly, releases capture device, disconnects MQTT
- Logs each step: step number, action type, Claude's raw response

## HID Action Schema
All actions are JSON. The ESP32 and the Claude system prompt both use this schema:
```json
{ "type": "move",        "dx": 10,   "dy": -5 }
{ "type": "click",       "button": "left" }
{ "type": "right_click", "button": "right" }
{ "type": "double_click","button": "left" }
{ "type": "scroll",      "dx": 0,    "dy": -3 }
{ "type": "key",         "keys": ["ctrl", "c"] }
{ "type": "type",        "text": "hello world" }
{ "type": "screenshot" }
{ "type": "done",        "message": "task complete" }
```

Note: mouse movement is **relative** (dx/dy deltas), not absolute coordinates.
Claude must navigate by moving incrementally and taking screenshots to verify
position. This is simpler and more reliable for HID than absolute positioning.

## ESP32 Firmware (esp32/firmware/main.cpp)

### Libraries (via PlatformIO)
- `USB` and `USBHIDMouse`, `USBHIDKeyboard` — native S3 USB HID
- `PubSubClient` — MQTT client
- `WiFi` — WiFi connection
- `ArduinoJson` — JSON parsing
- `WiFiClientSecure` — TLS support

### platformio.ini
```ini
[env:esp32-s3-devkitc-1]
platform = espressif32
board = esp32-s3-devkitc-1
framework = arduino
lib_deps =
    knolleary/PubSubClient
    bblanchon/ArduinoJson
monitor_speed = 115200
build_flags =
    -D ARDUINO_USB_MODE=0
    -D ARDUINO_USB_CDC_ON_BOOT=0
```

### main.cpp behaviour
- On boot: connect WiFi, connect MQTT broker with TLS, begin USB HID
- Subscribe to `agent/hid/cmd`
- On message received:
  - Parse JSON
  - Execute the action:
    - move → Mouse.move(dx, dy, 0)
    - click → Mouse.click(MOUSE_LEFT), release after 50ms
    - right_click → Mouse.click(MOUSE_RIGHT)
    - double_click → two clicks 80ms apart
    - scroll → Mouse.move(0, 0, dy)
    - key → map key names to USB HID keycodes, Keyboard.press/release
    - type → Keyboard.print(text)
  - Publish `{"status":"ok","type":<echoed type>}` to `agent/hid/ack`
- Keep MQTT connection alive with loop() and reconnect logic
- Serial.println each received command for debug monitoring
- WiFi credentials and MQTT credentials stored in a secrets.h file
  (gitignored), structured as #define constants

### Key mapping for "key" action
Support at minimum: ctrl, alt, shift, win, tab, enter, escape, backspace,
delete, up, down, left, right, f1–f12, home, end, pageup, pagedown, space.
Map these string names to their Arduino USB HID keycodes.

## Tests (tests/)

### test_screen.py
- Opens capture device, grabs 3 frames, asserts each is a valid PIL Image
- Asserts dimensions are SCREEN_WIDTH × SCREEN_HEIGHT
- Asserts b64 output is a non-empty string that decodes without error
- Uses pytest, marks as `@pytest.mark.hardware` (skipped in CI)

### test_hid.py
- Publishes each action type to the MQTT broker
- Asserts ACK received within 3 seconds for each
- Verifies echoed type in ACK matches sent type
- Marks as `@pytest.mark.hardware`

### test_agent_loop.py
- Mocks ClaudeClient.decide() to return a fixed sequence of actions
- Mocks HIDPublisher.send() to record calls
- Mocks ScreenCapture.grab() to return a blank image
- Asserts the loop calls send() for each non-screenshot, non-done action
- Asserts loop terminates on "done" action
- Asserts loop respects MAX_STEPS limit
- No hardware required — runs in CI

## Environment variables required
```
ANTHROPIC_API_KEY
MQTT_BROKER
MQTT_USERNAME
MQTT_PASSWORD
CAPTURE_DEVICE_ID   (optional, default 0)
```

## Implementation notes for Claude Code
- Use `python-dotenv` to load a `.env` file so the developer doesn't
  have to export vars manually during development
- All classes should be independently testable with mocks
- No global state — pass config explicitly
- Type hints throughout
- The ESP32 firmware is Arduino/C++ — implement in main.cpp only,
  keep it readable and well-commented since it runs on constrained hardware
- Do not use pyautogui anywhere — all input goes through MQTT → ESP32
- OpenCV is used only for frame capture; Pillow handles image processing
- Relative mouse movement means Claude needs a "home" strategy —
  implement a `home_mouse()` helper in main.py that sends the mouse to
  top-left by moving a large delta (e.g. dx=-5000, dy=-5000) before
  each task starts, giving Claude a known starting position
