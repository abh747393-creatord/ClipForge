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


def _log_usage(model, purpose, prompt, reply, usage=None):
    """Append one AI-call record to data/usage.jsonl (PeakCut usage page).
    Token counts come from the provider response when present, else a
    rough chars/4 estimate. Never raises."""
    try:
        tin = tout = 0
        if isinstance(usage, dict):
            tin = int(usage.get("prompt_tokens") or 0)
            tout = int(usage.get("completion_tokens") or 0)
        if not tin:
            tin = max(1, len(str(prompt or "")) // 4)
        if not tout:
            tout = max(1, len(str(reply or "")) // 4)
        rec = {"t": time.time(), "model": str(model or ""), "purpose": str(purpose or "llm"),
               "tokens_in": tin, "tokens_out": tout}
        p = config.DATA_DIR / "usage.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def usage_summary() -> dict:
    """AI usage totals: today, all-time, by purpose, last 30 days, recent calls."""
    out = {"today": {"calls": 0, "tokens_in": 0, "tokens_out": 0},
           "total": {"calls": 0, "tokens_in": 0, "tokens_out": 0},
           "by_purpose": {}, "daily": {}, "recent": []}
    try:
        p = config.DATA_DIR / "usage.jsonl"
        if not p.is_file():
            return out
        today = time.strftime("%Y-%m-%d", time.localtime())
        rows = []
        for line in p.read_text(encoding="utf-8").splitlines()[-5000:]:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        for r in rows:
            tin, tout = int(r.get("tokens_in") or 0), int(r.get("tokens_out") or 0)
            out["total"]["calls"] += 1
            out["total"]["tokens_in"] += tin
            out["total"]["tokens_out"] += tout
            day = time.strftime("%Y-%m-%d", time.localtime(float(r.get("t") or 0)))
            d = out["daily"].setdefault(day, {"calls": 0, "tokens_in": 0, "tokens_out": 0})
            d["calls"] += 1
            d["tokens_in"] += tin
            d["tokens_out"] += tout
            if day == today:
                out["today"]["calls"] += 1
                out["today"]["tokens_in"] += tin
                out["today"]["tokens_out"] += tout
            bp = out["by_purpose"].setdefault(str(r.get("purpose") or "llm"),
                                              {"calls": 0, "tokens_in": 0, "tokens_out": 0})
            bp["calls"] += 1
            bp["tokens_in"] += tin
            bp["tokens_out"] += tout
        out["daily"] = dict(sorted(out["daily"].items())[-30:])
        out["recent"] = rows[-15:][::-1]
    except Exception:
        pass
    return out


def complete(prompt, max_tokens=400, temperature=0.3, purpose="llm"):
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
        if isinstance(content, str) and content.strip():
            _log_usage(model, purpose, prompt, content, data.get("usage"))
            return content
        return None
    except Exception:
        return None


def complete_vision(prompt, image_paths, max_tokens=300, temperature=0.2, purpose="vision"):
    """One vision chat completion (OpenAI-compatible image_url parts, base64).
    Returns the message content or None on any failure (e.g. text-only model)."""
    try:
        import base64
        from pathlib import Path
        s = settings()
        base = (s.get("base_url") or _DEFAULT_BASE).rstrip("/")
        model = s.get("model") or _DEFAULT_MODEL
        headers = {"Content-Type": "application/json"}
        if s.get("key"):
            headers["Authorization"] = f"Bearer {s['key']}"
        parts = [{"type": "text", "text": str(prompt)}]
        for pth in image_paths or []:
            p = Path(str(pth))
            if not p.is_file():
                continue
            mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
            b64 = base64.b64encode(p.read_bytes()).decode("ascii")
            parts.append({"type": "image_url",
                          "image_url": {"url": f"data:{mime};base64,{b64}"}})
        if len(parts) < 2:
            return None
        payload = {"model": model,
                   "messages": [{"role": "user", "content": parts}],
                   "max_tokens": int(max_tokens), "temperature": float(temperature),
                   "stream": False}
        with _client(120.0) as c:
            r = c.post(f"{base}/chat/completions", json=payload, headers=headers)
        if r.status_code != 200:
            return None
        data = r.json()
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
        if isinstance(content, str) and content.strip():
            _log_usage(model, purpose, prompt, content, data.get("usage"))
            return content
        return None
    except Exception:
        return None


def complete_json(prompt, max_tokens=500, temperature=0.25, purpose="llm"):
    """complete() + tolerant JSON extraction: strips ``` fences, finds the
    first {...} block, json.loads it. Returns dict or None on any failure."""
    text = complete(prompt, max_tokens=max_tokens, temperature=temperature, purpose=purpose)
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
