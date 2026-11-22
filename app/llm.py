"""Optional LLM access (OpenAI-compatible chat completions).

Works with a local Ollama (default http://127.0.0.1:11434/v1, model
llama3.2:3b) or any configured cloud endpoint. Every function is fail-soft:
complete()/complete_json() return None on ANY failure and never raise.
"""
import json
import re
import time

from . import config

_DEFAULT_BASE = "http://127.0.0.1:11434/v1"
_DEFAULT_MODEL = "llama3.2:3b"
_OLLAMA_TAGS = "http://127.0.0.1:11434/api/tags"

_avail_cache = {"at": 0.0, "ok": False}


def settings() -> dict:
    """{"base_url", "model", "key"} from config (env + settings.json)."""
    return config.llm_settings()


def available() -> bool:
    """True if a base_url is configured, or a local Ollama answers
    /api/tags (short timeout; result cached for 60 s)."""
    if settings().get("base_url"):
        return True
    now = time.time()
    if now - _avail_cache["at"] < 60:
        return _avail_cache["ok"]
    ok = False
    try:
        import httpx
        r = httpx.get(_OLLAMA_TAGS, timeout=2.0)
        ok = r.status_code == 200
    except Exception:
        ok = False
    _avail_cache.update(at=now, ok=ok)
    return ok


def complete(prompt, max_tokens=400, temperature=0.3):
    """One chat completion → message content string, or None on any failure."""
    try:
        import httpx
        s = settings()
        base = (s.get("base_url") or _DEFAULT_BASE).rstrip("/")
        model = s.get("model") or _DEFAULT_MODEL
        headers = {"Content-Type": "application/json"}
        if s.get("key"):
            headers["Authorization"] = f"Bearer {s['key']}"
        payload = {"model": model,
                   "messages": [{"role": "user", "content": str(prompt)}],
                   "max_tokens": int(max_tokens), "temperature": float(temperature),
                   "stream": False}
        r = httpx.post(f"{base}/chat/completions", json=payload, headers=headers, timeout=120.0)
        if r.status_code != 200:
            return None
        data = r.json()
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
        return content if isinstance(content, str) and content.strip() else None
    except Exception:
        return None


def complete_json(prompt, max_tokens=500, temperature=0.25):
    """complete() + tolerant JSON extraction: strips ``` fences, finds the
    first {...} block, json.loads it. Returns dict or None on any failure."""
    text = complete(prompt, max_tokens=max_tokens, temperature=temperature)
    if not text:
        return None
    try:
        cleaned = re.sub(r"```(?:json)?", "", text)
        m = re.search(r"\{.*\}", cleaned, re.S)
        if not m:
            return None
        out = json.loads(m.group(0))
        return out if isinstance(out, dict) else None
    except Exception:
        return None
