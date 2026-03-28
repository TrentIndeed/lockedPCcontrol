# Project: Locked PC Control Agent

## Overview
AI agent that controls a remote Windows PC through physical HID input.
No software is installed on the target PC. Control is achieved by:
- Reading the screen via HDMI capture card (USB webcam)
- Sending mouse/keyboard via ESP32-S3 acting as USB HID device
- Serial communication (COM port) between agent PC and ESP32
- OpenAI-compatible API (via proxy) as the AI brain
- Flask + SocketIO web UI at localhost:5000

## Architecture
- **agent/web.py** — Main entry point. Flask server, agent loop, HID execution, calibration
- **agent/claude_client.py** — AI client, prompt construction, stuck/backward detection
- **agent/hid_publisher.py** — Serial JSON protocol to ESP32, ACK-based flow control
- **agent/screen.py** — OpenCV capture card reading, PIL image conversion
- **agent/config.py** — YAML config loader (agent.yaml > agent.local.yaml > env vars)
- **agent/system_prompt.txt** — Editable AI system prompt (generic, uses {w}/{h} placeholders)
- **agent/profiles/** — Task-specific prompt additions (e.g., elearning.txt)
- **esp32/src/main.cpp** — ESP32-S3 firmware: USB HID + serial JSON command handler

## Key Design Decisions
- Mouse uses **home-then-move** absolute positioning (send to 0,0 then move to target)
- Mickey scale converts image pixels to HID mickeys (depends on target PC pointer speed)
- ESP32 chunks large moves at 127 mickeys per HID report with 5ms delay
- Stuck detection: buckets click coordinates to 40px grid, flags after 2 repeated clicks
- Backward detection: compares OBSERVE text similarity against earlier history
- Profiles: optional .txt files in profiles/ appended to system prompt for task-specific rules
- Session notes: persistent user-provided context injected with every task

## Config
All settings in agent.yaml. Override with agent.local.yaml or env vars.
No secrets in the repo. API endpoint is a local proxy.
