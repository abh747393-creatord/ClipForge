"""ClipForge configuration: paths, server settings, ASR/LLM defaults.

Environment variables (all optional):
  CLIPFORGE_DATA_DIR     data directory (default: <root>/data)
  CLIPFORGE_HOST         bind host (default: 127.0.0.1)
  CLIPFORGE_PORT         bind port (default: 8780)
  CLIPFORGE_ASR_MODEL    faster-whisper model spec (default: "base")
  CLIPFORGE_LLM_BASE_URL OpenAI-compatible base URL (e.g. http://127.0.0.1:11434/v1)
  CLIPFORGE_LLM_MODEL    chat model name
  CLIPFORGE_LLM_KEY      API key (if the endpoint needs one)

DATA_DIR/settings.json may override the LLM settings with keys
llm_base_url / llm_model / llm_key (written by the settings UI).
"""
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # the clipforge root
DATA_DIR = Path(os.environ.get("CLIPFORGE_DATA_DIR", BASE_DIR / "data"))
FONTS_DIR = BASE_DIR / "fonts"

HOST = os.environ.get("CLIPFORGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("CLIPFORGE_PORT", "8780"))

# Default vertical output geometry (9:16).
OUT_W, OUT_H, OUT_FPS = 1080, 1920, 30

ASR_MODEL = os.environ.get("CLIPFORGE_ASR_MODEL", "base")


def llm_settings() -> dict:
    """LLM connection settings: {"base_url", "model", "key"}.

    Env vars provide the defaults; DATA_DIR/settings.json keys
    (llm_base_url / llm_model / llm_key) override them when present.
    Never raises — a missing or unreadable settings file is ignored.
    """
    out = {
        "base_url": os.environ.get("CLIPFORGE_LLM_BASE_URL", ""),
        "model": os.environ.get("CLIPFORGE_LLM_MODEL", ""),
        "key": os.environ.get("CLIPFORGE_LLM_KEY", ""),
    }
    try:
        saved = json.loads((DATA_DIR / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    if isinstance(saved, dict):
        for src, dst in (("llm_base_url", "base_url"), ("llm_model", "model"), ("llm_key", "key")):
            val = saved.get(src)
            if isinstance(val, str) and val.strip():
                out[dst] = val.strip()
    return out


def ensure_dirs():
    """Create DATA_DIR and DATA_DIR/jobs if they don't exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "jobs").mkdir(parents=True, exist_ok=True)
