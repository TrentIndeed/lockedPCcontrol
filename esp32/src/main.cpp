/*
 * ESP32-S3 USB HID Agent Firmware — Serial control
 *
 * Receives JSON commands over the USB-CDC serial port (connected to agent PC)
 * and executes them as USB HID mouse/keyboard actions on the locked PC
 * via the USB OTG port.
 *
 * Wiring:
 *   USB-CDC (COM5) ──→ Agent PC (Python sends commands here)
 *   USB OTG         ──→ Locked PC (HID mouse/keyboard output)
 */

#include <Arduino.h>
#include <ArduinoJson.h>
#include <USB.h>
#include <USBHIDMouse.h>
#include <USBHIDKeyboard.h>

// --- Global objects --------------------------------------------------------
USBHIDMouse    Mouse;
USBHIDKeyboard Keyboard;

// --- Forward declarations --------------------------------------------------
void processLine(const char* line);
void handleAction(JsonDocument& doc);
void sendAck(const char* actionType);
uint8_t mapKey(const char* name);

// Serial read buffer
static char lineBuf[1024];
static int  linePos = 0;

// ==========================================================================
//  Setup
// ==========================================================================
void setup() {
    Serial.begin(115200);
    delay(1000);
    Serial.println("[Boot] ESP32-S3 HID Agent starting…");

    // USB HID on the OTG port
    USB.begin();
    Mouse.begin();
    Keyboard.begin();
    delay(500);

    Serial.println("[Ready] Waiting for commands on Serial…");
}

// ==========================================================================
//  Loop — read newline-delimited JSON from Serial
// ==========================================================================
void loop() {
    while (Serial.available()) {
        char c = Serial.read();
        if (c == '\n' || c == '\r') {
            if (linePos > 0) {
                lineBuf[linePos] = '\0';
                processLine(lineBuf);
                linePos = 0;
            }
        } else if (linePos < (int)(sizeof(lineBuf) - 1)) {
            lineBuf[linePos++] = c;
        }
    }
}

// ==========================================================================
//  Process one JSON line
// ==========================================================================
void processLine(const char* line) {
    Serial.printf("[CMD] %s\n", line);

    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, line);
    if (err) {
        Serial.printf("[JSON] Parse error: %s\n", err.c_str());
        return;
    }

    handleAction(doc);
}

// ==========================================================================
//  Action dispatcher
// ==========================================================================
void handleAction(JsonDocument& doc) {
    const char* type = doc["type"] | "unknown";

    // --- move --------------------------------------------------------------
    if (strcmp(type, "move") == 0) {
        int dx = doc["dx"] | 0;
        int dy = doc["dy"] | 0;
        // Mouse.move() takes int8_t, so move in chunks of 127 max
        while (dx != 0 || dy != 0) {
            int stepX = constrain(dx, -127, 127);
            int stepY = constrain(dy, -127, 127);
            Mouse.move(stepX, stepY, 0);
            dx -= stepX;
            dy -= stepY;
            if (dx != 0 || dy != 0) delay(5);
        }
    }
    // --- click -------------------------------------------------------------
    else if (strcmp(type, "click") == 0) {
        Mouse.click(MOUSE_LEFT);
    }
    // --- right_click -------------------------------------------------------
    else if (strcmp(type, "right_click") == 0) {
        Mouse.click(MOUSE_RIGHT);
    }
    // --- double_click ------------------------------------------------------
    else if (strcmp(type, "double_click") == 0) {
        Mouse.click(MOUSE_LEFT);
        delay(80);
        Mouse.click(MOUSE_LEFT);
    }
    // --- scroll ------------------------------------------------------------
    else if (strcmp(type, "scroll") == 0) {
        int dy = doc["dy"] | 0;
        Mouse.move(0, 0, dy);
    }
    // --- key ---------------------------------------------------------------
    else if (strcmp(type, "key") == 0) {
        JsonArray keys = doc["keys"].as<JsonArray>();
        for (JsonVariant k : keys) {
            uint8_t code = mapKey(k.as<const char*>());
            if (code) Keyboard.press(code);
        }
        delay(50);
        Keyboard.releaseAll();
    }
    // --- type --------------------------------------------------------------
    else if (strcmp(type, "type") == 0) {
        const char* text = doc["text"] | "";
        Keyboard.print(text);
    }
    // --- screenshot / done — no HID action --------------------------------
    else if (strcmp(type, "screenshot") == 0 || strcmp(type, "done") == 0) {
        // nothing to do
    }
    else {
        Serial.printf("[Action] Unknown type: %s\n", type);
    }

    sendAck(type);
}

// ==========================================================================
//  Send ACK back over Serial as JSON
// ==========================================================================
void sendAck(const char* actionType) {
    char ack[128];
    snprintf(ack, sizeof(ack), "{\"status\":\"ok\",\"type\":\"%s\"}", actionType);
    Serial.println(ack);
}

// ==========================================================================
//  Key name → USB HID keycode mapping
// ==========================================================================
uint8_t mapKey(const char* name) {
    if (!name) return 0;

    // modifiers
    if (strcmp(name, "ctrl")  == 0) return KEY_LEFT_CTRL;
    if (strcmp(name, "alt")   == 0) return KEY_LEFT_ALT;
    if (strcmp(name, "shift") == 0) return KEY_LEFT_SHIFT;
    if (strcmp(name, "win")   == 0) return KEY_LEFT_GUI;

    // special keys
    if (strcmp(name, "tab")       == 0) return KEY_TAB;
    if (strcmp(name, "enter")     == 0) return KEY_RETURN;
    if (strcmp(name, "escape")    == 0) return KEY_ESC;
    if (strcmp(name, "backspace") == 0) return KEY_BACKSPACE;
    if (strcmp(name, "delete")    == 0) return KEY_DELETE;
    if (strcmp(name, "space")     == 0) return ' ';

    // arrow keys
    if (strcmp(name, "up")    == 0) return KEY_UP_ARROW;
    if (strcmp(name, "down")  == 0) return KEY_DOWN_ARROW;
    if (strcmp(name, "left")  == 0) return KEY_LEFT_ARROW;
    if (strcmp(name, "right") == 0) return KEY_RIGHT_ARROW;

    // navigation
    if (strcmp(name, "home")     == 0) return KEY_HOME;
    if (strcmp(name, "end")      == 0) return KEY_END;
    if (strcmp(name, "pageup")   == 0) return KEY_PAGE_UP;
    if (strcmp(name, "pagedown") == 0) return KEY_PAGE_DOWN;

    // function keys F1–F12
    if (strcmp(name, "f1")  == 0) return KEY_F1;
    if (strcmp(name, "f2")  == 0) return KEY_F2;
    if (strcmp(name, "f3")  == 0) return KEY_F3;
    if (strcmp(name, "f4")  == 0) return KEY_F4;
    if (strcmp(name, "f5")  == 0) return KEY_F5;
    if (strcmp(name, "f6")  == 0) return KEY_F6;
    if (strcmp(name, "f7")  == 0) return KEY_F7;
    if (strcmp(name, "f8")  == 0) return KEY_F8;
    if (strcmp(name, "f9")  == 0) return KEY_F9;
    if (strcmp(name, "f10") == 0) return KEY_F10;
    if (strcmp(name, "f11") == 0) return KEY_F11;
    if (strcmp(name, "f12") == 0) return KEY_F12;

    // single printable character
    if (strlen(name) == 1) return (uint8_t)name[0];

    Serial.printf("[Key] Unknown key name: %s\n", name);
    return 0;
}
