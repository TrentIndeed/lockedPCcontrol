"""Configuration — loads from agent.yaml, agent.local.yaml, and env vars.

Priority (highest wins): environment variables > agent.local.yaml > agent.yaml
"""

import os

from dotenv import load_dotenv

# Load .env from project root (one level up from agent/)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

# ---------------------------------------------------------------------------
# Load YAML config
# ---------------------------------------------------------------------------

def _load_yaml(path: str) -> dict:
    """Minimal YAML loader — handles only flat key: value pairs."""
    data: dict = {}
    if not os.path.exists(path):
        return data
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip()
            # strip inline comments (but not inside quotes)
            if val and val[0] not in ('"', "'"):
                val = val.split("#")[0]
            val = val.strip().strip('"').strip("'")
            if not key or not val:
                continue
            data[key] = val
    return data


_BASE_DIR = os.path.dirname(__file__)
_cfg = _load_yaml(os.path.join(_BASE_DIR, "agent.yaml"))
_cfg.update(_load_yaml(os.path.join(_BASE_DIR, "agent.local.yaml")))


def _get(key: str, default: str = "") -> str:
    """Get config value: env var > local yaml > base yaml > default."""
    env_key = key.upper()
    return os.environ.get(env_key, _cfg.get(key, default))


# ---------------------------------------------------------------------------
# Config values
# ---------------------------------------------------------------------------

# AI
API_BASE_URL: str = _get("api_base_url", "http://127.0.0.1:10531/v1")
MODEL: str = _get("model", "gpt-5.4")
MAX_OUTPUT_TOKENS: int = int(_get("max_output_tokens", "1024"))
MAX_STEPS: int = int(_get("max_steps", "200"))

# Hardware
SERIAL_PORT: str = _get("serial_port", "COM5")
SERIAL_BAUD: int = int(_get("serial_baud", "115200"))
CAPTURE_DEVICE_ID: int = int(_get("capture_device_id", "1"))

# Screen
SCREEN_WIDTH: int = int(_get("screen_width", "1280"))
SCREEN_HEIGHT: int = int(_get("screen_height", "720"))

# Mouse scale
MICKEY_SCALE_X: float = float(_get("mickey_scale_x", "0.79"))
MICKEY_SCALE_Y: float = float(_get("mickey_scale_y", "0.79"))

# Timing
PROMPT_RATE: float = float(_get("prompt_rate", "5.0"))

# ---------------------------------------------------------------------------
# System prompt — loaded from system_prompt.txt if it exists
# ---------------------------------------------------------------------------

_PROMPT_FILE = os.path.join(_BASE_DIR, "system_prompt.txt")

if os.path.exists(_PROMPT_FILE):
    with open(_PROMPT_FILE, "r", encoding="utf-8") as f:
        SYSTEM_PROMPT: str = f.read()
else:
    SYSTEM_PROMPT: str = ""

# ---------------------------------------------------------------------------
# Task profiles — optional extra instructions appended to the system prompt
# ---------------------------------------------------------------------------

PROFILES_DIR: str = os.path.join(_BASE_DIR, "profiles")


def list_profiles() -> list[str]:
    """Return available profile names (filename without extension)."""
    if not os.path.isdir(PROFILES_DIR):
        return []
    return sorted(
        os.path.splitext(f)[0]
        for f in os.listdir(PROFILES_DIR)
        if f.endswith(".txt")
    )


def load_profile(name: str) -> str:
    """Load a profile's text by name. Returns empty string if not found."""
    path = os.path.join(PROFILES_DIR, f"{name}.txt")
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


_LEARNED_HEADER = "LEARNED FROM EXPERIENCE:"
_MAX_LESSONS = 10


def add_lesson(profile_name: str, lesson: str) -> bool:
    """Append a one-line lesson to a profile's LEARNED section.

    Deduplicates by checking word overlap with existing lessons.
    Keeps at most _MAX_LESSONS entries (FIFO).
    Returns True if the lesson was added.
    """
    if not profile_name:
        return False
    path = os.path.join(PROFILES_DIR, f"{profile_name}.txt")
    if not os.path.exists(path):
        return False

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    # Parse existing lessons
    lessons: list[str] = []
    if _LEARNED_HEADER in content:
        base, _, learned_block = content.partition(_LEARNED_HEADER)
        base = base.rstrip()
        for line in learned_block.strip().splitlines():
            line = line.strip()
            if line.startswith("- "):
                lessons.append(line[2:])
    else:
        base = content.rstrip()

    # Deduplicate — skip if >50% word overlap with any existing lesson
    new_words = set(lesson.lower().split())
    for existing in lessons:
        existing_words = set(existing.lower().split())
        if not new_words or not existing_words:
            continue
        overlap = len(new_words & existing_words) / min(len(new_words), len(existing_words))
        if overlap > 0.5:
            return False  # too similar to existing

    # Add and cap
    lessons.append(lesson)
    if len(lessons) > _MAX_LESSONS:
        lessons = lessons[-_MAX_LESSONS:]

    # Write back
    learned_text = "\n".join(f"- {l}" for l in lessons)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"{base}\n\n{_LEARNED_HEADER}\n{learned_text}\n")

    return True
