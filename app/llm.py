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

# Provider presets (PeakCut ai.PROVIDERS): all OpenAI-compatible endpoints.
PROVIDERS = [
    {"id": "ollama", "label": "Ollama (local, free)", "base_url": "http://127.0.0.1:11434/v1",
     "model_hint": "llama3.2:3b", "needs_key": False},
    {"id": "gemini", "label": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
     "model_hint": "gemini-2.0-flash", "needs_key": True},
    {"id": "openai", "label": "OpenAI", "base_url": "https://api.openai.com/v1",
     "model_hint": "gpt-4o-mini", "needs_key": True},
    {"id": "groq", "label": "Groq", "base_url": "https://api.groq.com/openai/v1",
     "model_hint": "llama-3.3-70b-versatile", "needs_key": True},
    {"id": "openrouter", "label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
     "model_hint": "meta-llama/llama-3.3-70b-instruct", "needs_key": True},
    {"id": "xai", "label": "xAI Grok", "base_url": "https://api.x.ai/v1",
     "model_hint": "grok-3-mini", "needs_key": True},
    {"id": "lmstudio", "label": "LM Studio (local)", "base_url": "http://127.0.0.1:1234/v1",
     "model_hint": "", "needs_key": False},
    {"id": "custom", "label": "Custom OpenAI-compatible", "base_url": "",
     "model_hint": "", "needs_key": False},
]


def _client(timeout):
    """httpx client that ignores proxy env vars: a local tool talks to the
    endpoint directly, and a malformed NO_PROXY (bracketed IPv6) otherwise
    crashes every call with 'Invalid port'."""
    import httpx
    return httpx.Client(trust_env=False, timeout=timeout)


def list_models(base_url, key=""):
    """GET {base}/models → list of model ids (PeakCut 'Find models').
    Returns [] on any failure. Never raises."""
    try:
        base = str(base_url or _DEFAULT_BASE).rstrip("/")
        headers = {}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        with _client(15.0) as c:
            r = c.get(f"{base}/models", headers=headers)
        if r.status_code != 200:
            return []
        data = r.json()
        items = data.get("data") if isinstance(data, dict) else data
        out = []
        for it in items or []:
            mid = it.get("id") if isinstance(it, dict) else str(it)
            if mid:
                out.append(str(mid))
        return out[:200]
    except Exception:
        return []


def test(base_url, model, key=""):
    """One tiny completion against the given endpoint (Settings 'Save and
    test'). Returns {"ok": bool, "reply": str, "error": str}. Never raises."""
    try:
        base = str(base_url or _DEFAULT_BASE).rstrip("/")
        mdl = str(model or _DEFAULT_MODEL)
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload = {"model": mdl,
                   "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
                   "max_tokens": 10, "temperature": 0}
        with _client(60.0) as c:
            r = c.post(f"{base}/chat/completions", json=payload, headers=headers)
        if r.status_code != 200:
            return {"ok": False, "reply": "", "error": f"HTTP {r.status_code}: {(r.text or '')[:200]}"}
        data = r.json()
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        return {"ok": bool(str(content).strip()), "reply": str(content)[:100], "error": ""}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reply": "", "error": str(e)[:300]}

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
        with _client(2.0) as c:
            r = c.get(_OLLAMA_TAGS)
        ok = r.status_code == 200
    except Exception:
        ok = False
    _avail_cache.update(at=now, ok=ok)
    return ok


def complete(prompt, max_tokens=400, temperature=0.3):
    """One chat completion → message content string, or None on any failure."""
    try:
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
        with _client(120.0) as c:
            r = c.post(f"{base}/chat/completions", json=payload, headers=headers)
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
