"""Small operational utilities: readiness, storage, updates, diagnostics,
notifications, option profiles, and static (ad-break) range detection.

Everything here is fail-soft: readiness() / update_check() /
diagnostics() / notify() / static_ranges() never raise, so the settings
UI can always render something truthful. Stdlib only (plus whatever the
checks probe: ffmpeg, faster-whisper, yt-dlp).
"""
import json
import os
import platform
import re
import shutil
import subprocess
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_ENCODERS = ("h264_nvenc", "h264_amf", "h264_qsv", "h264_videotoolbox", "libx264")
_VERSION_URL = "https://raw.githubusercontent.com/abh747393-creatord/ClipForge/main/VERSION"
_WALK_CAP = 50000  # max files counted by storage_info()'s size walk


def _run(cmd, timeout=30):
    """subprocess.run with ClipForge's usual capture settings."""
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, creationflags=_NO_WINDOW)


# ---------------------------------------------------------------- readiness
def _check(fn) -> dict:
    """Run one readiness check; any failure becomes {ok: False, detail: ...}."""
    try:
        ok, detail = fn()
        return {"ok": bool(ok), "detail": str(detail)}
    except Exception as exc:  # noqa: BLE001 - readiness must never raise
        return {"ok": False, "detail": str(exc)[:200]}


def _binary_check(binary):
    def check():
        path = shutil.which(binary)
        if not path:
            return False, f"{binary} not found on PATH"
        proc = _run([path, "-version"], timeout=15)
        lines = (proc.stdout or proc.stderr or "").strip().splitlines()
        return proc.returncode == 0, (lines[0][:120] if lines else path)
    return check


def _encoder_check():
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False, "ffmpeg not found"
    proc = _run([ffmpeg, "-hide_banner", "-encoders"], timeout=30)
    text = proc.stdout or ""
    found = [name for name in _ENCODERS if re.search(rf"\b{re.escape(name)}\b", text)]
    if not found:
        return False, "no H.264 encoder found (need libx264 or a hardware encoder)"
    hardware = [n for n in found if n != "libx264"]
    detail = ", ".join(found) + (" (hardware available)" if hardware else " (software only)")
    return True, detail


def _whisper_model_cached() -> bool:
    """True when a faster-whisper model is already on disk (data/models or HF cache)."""
    model_dir = config.DATA_DIR / "models"
    try:
        if model_dir.is_dir() and any(model_dir.iterdir()):
            return True
    except OSError:
        pass
    hub = Path.home() / ".cache" / "huggingface" / "hub"
    try:
        return hub.is_dir() and any("faster-whisper" in p.name for p in hub.iterdir())
    except OSError:
        return False


def _whisper_check():
    try:
        import faster_whisper
        version = getattr(faster_whisper, "__version__", "?")
    except Exception as exc:  # noqa: BLE001
        return False, f"faster-whisper not importable: {exc}"[:200]
    where = "model cached" if _whisper_model_cached() else "model downloads on first use"
    return True, f"faster-whisper {version}, model '{config.ASR_MODEL}' ({where})"


def _fonts_check():
    fonts = sorted(config.FONTS_DIR.glob("*.ttf")) if config.FONTS_DIR.is_dir() else []
    return len(fonts) >= 3, f"{len(fonts)} caption font(s) in {config.FONTS_DIR}"


def _face_model_check():
    path = config.BASE_DIR / "models" / "face_yunet.onnx"  # see assets.py ASSETS
    if path.is_file() and path.stat().st_size > 0:
        return True, f"YuNet face model present ({path.stat().st_size // 1024} KB)"
    return False, "YuNet face model missing (downloads on first start)"


def _disk_check():
    target = config.DATA_DIR if config.DATA_DIR.exists() else config.BASE_DIR
    free_gb = shutil.disk_usage(target).free / (1024 ** 3)
    return free_gb > 5, f"{free_gb:.1f} GB free on {target}"


def _llm_check():
    settings = config.llm_settings()
    if settings.get("base_url") and settings.get("model"):
        return True, f"{settings['model']} @ {settings['base_url']}"
    return False, "no LLM configured (optional; scoring falls back to signals)"


def _yt_dlp_check():
    try:
        import yt_dlp
        return True, f"yt-dlp {yt_dlp.version.__version__}"
    except Exception as exc:  # noqa: BLE001
        return False, f"yt-dlp not importable: {exc}"[:200]


def _js_runtime_check():
    for name in ("deno", "node"):
        path = shutil.which(name)
        if path:
            return True, f"{name} at {path}"
    return False, "neither deno nor node found (yt-dlp may need one for some sites)"


def readiness() -> dict:
    """Per-component health: {key: {"ok": bool, "detail": str}}.

    Keys: ffmpeg, ffprobe, encoder, whisper, fonts, face_model, disk,
    llm, yt_dlp, deno_node. The encoder check is presence-only (parsed
    from `ffmpeg -encoders`, no test encode). Never raises."""
    return {
        "ffmpeg": _check(_binary_check("ffmpeg")),
        "ffprobe": _check(_binary_check("ffprobe")),
        "encoder": _check(_encoder_check),
        "whisper": _check(_whisper_check),
        "fonts": _check(_fonts_check),
        "face_model": _check(_face_model_check),
        "disk": _check(_disk_check),
        "llm": _check(_llm_check),
        "yt_dlp": _check(_yt_dlp_check),
        "deno_node": _check(_js_runtime_check),
    }


# ------------------------------------------------------------------ storage
def storage_info() -> dict:
    """{"data_dir", "total_gb", "free_gb", "used_by_data_gb", "jobs"}.

    used_by_data_gb sums file sizes under DATA_DIR (walk capped at
    _WALK_CAP files so a huge library cannot stall the settings page).
    Never raises."""
    info = {"data_dir": str(config.DATA_DIR), "total_gb": 0.0, "free_gb": 0.0,
            "used_by_data_gb": 0.0, "jobs": 0}
    try:
        target = config.DATA_DIR if config.DATA_DIR.exists() else config.BASE_DIR
        usage = shutil.disk_usage(target)
        info["total_gb"] = round(usage.total / (1024 ** 3), 2)
        info["free_gb"] = round(usage.free / (1024 ** 3), 2)
    except OSError:
        pass
    try:
        total, seen = 0, 0
        for root, _dirs, files in os.walk(config.DATA_DIR):
            for name in files:
                seen += 1
                if seen > _WALK_CAP:
                    break
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
            if seen > _WALK_CAP:
                break
        info["used_by_data_gb"] = round(total / (1024 ** 3), 3)
    except OSError:
        pass
    try:
        jobs_dir = config.DATA_DIR / "jobs"
        info["jobs"] = sum(1 for p in jobs_dir.iterdir() if p.is_dir()) \
            if jobs_dir.is_dir() else 0
    except OSError:
        pass
    return info


# ------------------------------------------------------------------ updates
def _version_tuple(value):
    """'1.2.3' -> (1, 2, 3); non-numeric pieces count as 0."""
    out = []
    for piece in re.split(r"[.\-+_]", str(value or "").strip()):
        digits = re.match(r"\d+", piece)
        out.append(int(digits.group(0)) if digits else 0)
    return tuple(out) or (0,)


def update_check() -> dict:
    """{"local", "latest", "update_available", "error"} vs the GitHub VERSION.

    The local version comes from <root>/VERSION ("0.0.0" when absent);
    the remote file is fetched with a 5 s timeout. Never raises."""
    local = "0.0.0"
    try:
        text = (config.BASE_DIR / "VERSION").read_text(encoding="utf-8").strip()
        if text:
            local = text.splitlines()[0].strip() or local
    except OSError:
        pass
    latest, error = None, None
    try:
        req = urllib.request.Request(_VERSION_URL, headers={"User-Agent": "ClipForge"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read(4096).decode("utf-8", "replace").strip()
        latest = body.splitlines()[0].strip() if body else None
        if not latest:
            error = "empty VERSION response"
    except Exception as exc:  # noqa: BLE001 - offline is a normal state
        error = str(exc)[:200]
    available = bool(latest) and _version_tuple(latest) > _version_tuple(local)
    return {"local": local, "latest": latest, "update_available": available, "error": error}


# -------------------------------------------------------------- diagnostics
def _tool_version(binary):
    path = shutil.which(binary)
    if not path:
        return "not found"
    try:
        proc = _run([path, "-version"], timeout=15)
        lines = (proc.stdout or "").strip().splitlines()
        return lines[0][:160] if lines else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _module_version(module_name):
    try:
        mod = __import__(module_name)
        if module_name == "yt_dlp":
            return str(mod.version.__version__)
        return str(getattr(mod, "__version__", "unknown"))
    except Exception:  # noqa: BLE001
        return "not installed"


def _recent_job_errors(limit=5):
    errors = []
    try:
        candidates = []
        for job_file in (config.DATA_DIR / "jobs").glob("*/job.json"):
            try:
                job = json.loads(job_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(job, dict) and job.get("status") == "error":
                candidates.append(job)
        candidates.sort(key=lambda j: str(j.get("created") or ""), reverse=True)
        for job in candidates[:limit]:
            errors.append({"id": str(job.get("id") or ""),
                           "error": str(job.get("error") or "")[:300]})
    except OSError:
        pass
    return errors


def diagnostics() -> dict:
    """Redacted support snapshot (safe to paste into a bug report).

    Python/platform/tool versions, data dir + free disk, up to 5 recent
    job errors, and the LLM settings with the key reduced to set/not set.
    Never raises."""
    free_gb = 0.0
    try:
        target = config.DATA_DIR if config.DATA_DIR.exists() else config.BASE_DIR
        free_gb = round(shutil.disk_usage(target).free / (1024 ** 3), 2)
    except OSError:
        pass
    llm = config.llm_settings()
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "ffmpeg": _tool_version("ffmpeg"),
        "yt_dlp": _module_version("yt_dlp"),
        "faster_whisper": _module_version("faster_whisper"),
        "data_dir": str(config.DATA_DIR),
        "disk_free_gb": free_gb,
        "recent_errors": _recent_job_errors(),
        "settings": {
            "llm_base_url": llm.get("base_url") or "",
            "llm_model": llm.get("model") or "",
            "llm_key": "set" if (llm.get("key") or "").strip() else "not set",
        },
    }


# -------------------------------------------------------------- notify
def _read_settings() -> dict:
    try:
        data = json.loads((config.DATA_DIR / "settings.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _post_json(url, payload) -> None:
    """POST JSON with a 5 s timeout; raises on HTTP/transport failure."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "ClipForge"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        resp.read(4096)


def notify(event: str, text: str) -> dict:
    """Send a short notification to Discord and/or Telegram, if configured.

    Reads discord_webhook / telegram_token / telegram_chat_id from
    DATA_DIR/settings.json. The message is "[event] text" (just text
    when event is empty). Returns {"discord": bool|None,
    "telegram": bool|None, "errors": [...]} — None means that channel
    is not configured. Never raises."""
    result = {"discord": None, "telegram": None, "errors": []}
    settings = _read_settings()
    message = f"[{event}] {text}" if event else str(text)
    webhook = str(settings.get("discord_webhook") or "").strip()
    if webhook:
        try:
            _post_json(webhook, {"content": message[:2000]})
            result["discord"] = True
        except Exception as exc:  # noqa: BLE001
            result["discord"] = False
            result["errors"].append(f"discord: {exc}"[:200])
    token = str(settings.get("telegram_token") or "").strip()
    chat_id = str(settings.get("telegram_chat_id") or "").strip()
    if token and chat_id:
        try:
            _post_json(f"https://api.telegram.org/bot{token}/sendMessage",
                       {"chat_id": chat_id, "text": message[:4096]})
            result["telegram"] = True
        except Exception as exc:  # noqa: BLE001
            result["telegram"] = False
            result["errors"].append(f"telegram: {exc}"[:200])
    return result


# -------------------------------------------------------------- profiles
def _profiles_path() -> Path:
    return config.DATA_DIR / "profiles.json"


def _clean_profile_name(name) -> str:
    return str(name or "").strip()[:60]


def list_profiles() -> list:
    """Saved option profiles: [{"name", "options", "created"}]."""
    try:
        data = json.loads(_profiles_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if isinstance(item, dict) and item.get("name"):
            out.append({"name": str(item["name"]),
                        "options": item.get("options")
                        if isinstance(item.get("options"), dict) else {},
                        "created": str(item.get("created") or "")})
    return out


def _write_profiles(profiles) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = _profiles_path()
    tmp = path.parent / f"profiles.json.tmp{uuid.uuid4().hex[:8]}"
    tmp.write_text(json.dumps(profiles, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def save_profile(name, options) -> dict:
    """Save (or overwrite) a named option profile; returns the stored profile.

    Names are trimmed and capped at 60 chars; saving an existing name
    replaces its options and keeps the original created timestamp.
    Raises ValueError for an empty name."""
    clean = _clean_profile_name(name)
    if not clean:
        raise ValueError("profile name is empty")
    opts = dict(options) if isinstance(options, dict) else {}
    profiles = list_profiles()
    for profile in profiles:
        if profile["name"] == clean:
            profile["options"] = opts
            _write_profiles(profiles)
            return dict(profile)
    profile = {"name": clean, "options": opts,
               "created": datetime.now(timezone.utc).isoformat()}
    profiles.append(profile)
    _write_profiles(profiles)
    return dict(profile)


def delete_profile(name) -> bool:
    """Remove a profile by name; True when one was actually deleted."""
    clean = _clean_profile_name(name)
    if not clean:
        return False
    profiles = list_profiles()
    kept = [p for p in profiles if p["name"] != clean]
    if len(kept) == len(profiles):
        return False
    _write_profiles(kept)
    return True


# ---------------------------------------------------------- static ranges
_PTS_RX = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")
_YAVG_RX = re.compile(r"lavfi\.signalstats\.YAVG=([0-9]+(?:\.[0-9]+)?)")


def static_ranges(src, duration, threshold=0.4, min_seconds=6) -> list:
    """Static stretches of a video: [[start, end], ...] in seconds.

    PeakCut-style ad-break detection: frames are sampled at 2 fps, each
    sample is the pixel difference to the previous one (tblend), and a
    sample whose mean difference (signalstats YAVG) is under `threshold`
    counts as static. Consecutive static samples merge into ranges; only
    ranges at least `min_seconds` long survive. Any failure returns []."""
    try:
        duration = float(duration)
        threshold = float(threshold)
        min_seconds = float(min_seconds)
    except (TypeError, ValueError):
        return []
    if duration <= 0 or min_seconds <= 0:
        return []
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not Path(src).is_file():
        return []
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-i", str(src), "-an",
           "-vf", "fps=2,tblend=difference,signalstats,"
                  "metadata=print:key=lavfi.signalstats.YAVG",
           "-f", "null", "-"]
    try:
        proc = _run(cmd, timeout=600)
    except Exception:  # noqa: BLE001 - timeout / spawn failure: no ranges, not an error
        return []
    if proc.returncode != 0:
        return []
    samples = []  # (pts_time, yavg) of the difference stream
    pending = None
    for line in (proc.stderr or "").splitlines():
        match = _PTS_RX.search(line)
        if match:
            pending = float(match.group(1))
            continue
        match = _YAVG_RX.search(line)
        if match and pending is not None:
            samples.append((pending, float(match.group(1))))
            pending = None
    if not samples:
        return []
    step = 0.5  # fps=2 sampling interval
    ranges = []
    run_start = None
    run_last = None

    def flush(end_of_stream=False):
        if run_start is None:
            return
        start = max(0.0, run_start - step)
        end = run_last + step if end_of_stream else run_last
        end = min(end, duration)
        if end - start >= min_seconds:
            ranges.append([round(start, 2), round(end, 2)])

    for pts, yavg in samples:
        if yavg < threshold:
            if run_start is None:
                run_start = pts
            run_last = pts
        else:
            flush()
            run_start = run_last = None
    flush(end_of_stream=True)
    return ranges
