"""ClipForge HTTP server: JSON API + the no-build web UI.

Local tool, no auth. Settings (LLM endpoint/model/key) live in
DATA_DIR/settings.json as flat keys llm_base_url / llm_model / llm_key —
config.llm_settings() reads them. The API key is never returned by the API.
"""
import json
import shutil
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import assets, captions, config, facecam, jobs, layouts, live, llm, media, presets, publish

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
VERSION = "1.0.0"
UPLOAD_EXTS = {".mp4", ".mov", ".mkv", ".webm"}
STATIC_FILES = {"style.css", "app.js"}

config.ensure_dirs()
store = jobs.JobStore()
store.load_existing()

app = FastAPI(title="ClipForge")


# ---------------------------------------------------------------------- errors
@app.exception_handler(StarletteHTTPException)
async def _http_error(_request: Request, exc: StarletteHTTPException):
    return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def _validation_error(_request: Request, exc: RequestValidationError):
    return JSONResponse({"error": "invalid request: " + str(exc.errors()[:1])}, status_code=422)


@app.exception_handler(Exception)
async def _unhandled(_request: Request, exc: Exception):  # noqa: BLE001
    return JSONResponse({"error": str(exc)}, status_code=500)


# ---------------------------------------------------------------------- UI
@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/static/{name}")
async def static_file(name: str):
    if name not in STATIC_FILES:
        raise HTTPException(404, "not found")
    return FileResponse(WEB_DIR / name)


# ---------------------------------------------------------------------- meta
@app.get("/api/status")
async def status():
    return {
        "ok": True,
        "version": VERSION,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "asr_model": config.ASR_MODEL,
        "llm_available": bool(llm.available()),
    }


@app.get("/api/layouts")
async def list_layouts():
    return [{"id": c[0], "label": c[1], "desc": c[2]} for c in layouts.CATALOG]


@app.get("/api/styles")
async def list_styles():
    return {"styles": captions.styles_catalog(), "anims": list(captions.ANIMS)}


@app.get("/api/presets")
async def list_presets():
    return presets.catalog()


# ---------------------------------------------------------------------- discover
def _watchlist_path() -> Path:
    return config.DATA_DIR / "watchlist.txt"


def _watchlist_entries(topic=""):
    p = _watchlist_path()
    if not p.exists():
        return []
    out = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        who, _, only = line.partition("|")
        if only.strip() and topic and only.strip().lower() not in topic.lower():
            continue
        platform, _, handle = who.strip().partition(":")
        if platform in ("twitch", "youtube", "kick") and handle.strip():
            out.append((platform, handle.strip()))
    return out


@app.get("/api/watchlist")
async def get_watchlist():
    p = _watchlist_path()
    return {"text": p.read_text(encoding="utf-8") if p.exists() else ""}


@app.put("/api/watchlist")
async def put_watchlist(payload: dict):
    _watchlist_path().write_text(str(payload.get("text") or ""), encoding="utf-8")
    return {"ok": True}


@app.get("/api/discover")
async def discover(topic: str = "", when: str = "today", count: int = 8):
    """Biggest recent streams for a topic: YouTube search + your watchlist
    creators' newest matching videos (stream-clips discover)."""
    topic = (topic or "").strip()
    if not topic:
        raise HTTPException(400, "type a topic first (e.g. GTA 6)")
    if when not in ("hour", "today", "week", "month"):
        when = "today"
    count = max(1, min(20, int(count or 8)))
    words = {topic.lower(), topic.lower().replace(" ", "")}
    found = media.search_streams(topic, when=when, limit=20)
    for platform, handle in _watchlist_entries(topic):
        try:
            hit = media.watchlist_newest(platform, handle, sorted(words))
        except Exception:  # noqa: BLE001 - one bad creator must not sink the search
            hit = None
        if hit:
            found.append(hit)
    best = {}
    for item in found:
        if not any(w in (item.get("title") or "").lower() for w in words):
            continue
        key = (item.get("channel") or "").lower()
        if key not in best or item.get("views", 0) > best[key].get("views", 0):
            best[key] = item
    return sorted(best.values(), key=lambda s: -s.get("views", 0))[:count]


# ---------------------------------------------------------------------- jobs
@app.post("/api/jobs")
async def create_job(payload: dict):
    url = (payload.get("url") or "").strip()
    if not url or not media.site_allowed(url):
        raise HTTPException(400, "That link is not supported.")
    source = {"type": "url", "url": url, "path": None, "name": url}
    job = store.create(source, payload.get("options"))
    jobs.submit(job, store)
    return job


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), options: str = Form("{}")):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in UPLOAD_EXTS:
        raise HTTPException(400, "Only .mp4, .mov, .mkv and .webm files are supported.")
    try:
        opts = json.loads(options) if options else {}
    except ValueError:
        opts = {}
    uploads = config.DATA_DIR / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    dest = uploads / f"{uuid.uuid4().hex}{ext}"
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    source = {"type": "file", "url": None, "path": str(dest), "name": file.filename or dest.name}
    job = store.create(source, opts)
    jobs.submit(job, store)
    return job


@app.get("/api/jobs")
async def list_jobs():
    return [{
        "id": j["id"],
        "status": j.get("status"),
        "stage": j.get("stage"),
        "progress": j.get("progress"),
        "title": j.get("title") or (j.get("source") or {}).get("name") or "",
        "created": j.get("created"),
        "clips": len(j.get("clips") or []),
    } for j in store.list()]


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    if store.get(job_id) is None:
        raise HTTPException(404, "job not found")
    store.delete(job_id)
    return {"ok": True}


# ---------------------------------------------------------------------- media files
def _serve_from(base: Path, filename: str):
    """Serve `filename` from base or base/clips, refusing anything that
    resolves outside base."""
    if filename != Path(filename).name:
        raise HTTPException(404, "not found")
    base_resolved = base.resolve()
    for cand in (base / filename, base / "clips" / filename):
        try:
            resolved = cand.resolve()
        except OSError:
            continue
        if resolved.is_file() and resolved.is_relative_to(base_resolved):
            return FileResponse(resolved)
    raise HTTPException(404, "not found")


@app.get("/media/live/{session_id}/{filename}")
async def live_media(session_id: str, filename: str):
    if not session_id.isalnum():
        raise HTTPException(404, "not found")
    return _serve_from(config.DATA_DIR / "live" / session_id, filename)


@app.get("/media/{job_id}/{filename}")
async def job_media(job_id: str, filename: str):
    if not job_id.isalnum():
        raise HTTPException(404, "not found")
    return _serve_from(store.dir_for(job_id), filename)


# ---------------------------------------------------------------------- job actions
@app.post("/api/jobs/{job_id}/facecam/detect")
async def detect_facecam(job_id: str):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    src = job.get("source_path")
    if not src:
        clips = job.get("clips") or []
        src = clips[0].get("src") if clips else None
    if not src or not Path(src).exists():
        raise HTTPException(400, "no source video yet — wait for the download to finish")
    pr = media.probe(Path(src))
    det = facecam.detect(Path(src), float(pr.get("duration") or 0),
                         int(pr.get("width") or 0), int(pr.get("height") or 0))
    rect = None
    if det:
        rect = {"x": det["x"], "y": det["y"], "w": det["w"], "h": det["h"]}
        job.setdefault("options", {})["facecam"] = rect
        store.save(job)
    return {"facecam": rect}


@app.post("/api/jobs/{job_id}/clips/{idx}/rerender")
async def rerender(job_id: str, idx: int, payload: dict):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    try:
        return jobs.rerender_clip(job, store, idx, payload.get("options") or {})
    except IndexError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/jobs/{job_id}/prompts/{part}")
async def get_prompt(job_id: str, part: int):
    """One manual-AI prompt (transcript window) as plain text."""
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    p = store.dir_for(job_id) / "prompts" / f"part_{int(part):04d}.txt"
    if not p.is_file():
        raise HTTPException(404, "prompt not found")
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(p.read_text(encoding="utf-8"))


@app.post("/api/jobs/{job_id}/ai-answers")
async def ai_answers(job_id: str, payload: dict):
    """Submit manual-AI picks ([{start, end, title, hook, ...}]) and resume."""
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    picks = payload.get("picks")
    if picks is None and isinstance(payload.get("text"), str):
        # tolerate a pasted JSON blob (with or without code fences)
        import re
        m = re.search(r"\[.*\]", payload["text"], re.S)
        try:
            picks = json.loads(m.group(0)) if m else None
        except ValueError:
            picks = None
    try:
        return jobs.resume_with_picks(job, store, picks or [])
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/jobs/{job_id}/export.zip")
async def export_zip(job_id: str):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    jdir = store.dir_for(job_id)
    names = [c.get("name") or Path(c.get("file") or "").stem
             for c in (job.get("clips") or []) if c.get("status") == "done"]
    out = jdir / "export.zip"
    # export_zip looks for <name>.mp4/.jpg/... directly in the dir it is given;
    # renders live in the clips subdir.
    publish.export_zip(jdir / "clips", names, out)
    return FileResponse(out, media_type="application/zip", filename=f"clipforge-{job_id}.zip")


# ---------------------------------------------------------------------- live
@app.post("/api/live/start")
async def live_start(payload: dict):
    url = (payload.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "paste a live stream link first")
    session = live.start_session(url, payload.get("options"), store)
    return session.state()


@app.get("/api/live/{session_id}")
async def live_state(session_id: str):
    session = live.get_session(session_id)
    if session is None:
        raise HTTPException(404, "live session not found")
    return session.state()


@app.post("/api/live/{session_id}/clip-now")
async def live_clip_now(session_id: str, payload: dict):
    session = live.get_session(session_id)
    if session is None:
        raise HTTPException(404, "live session not found")
    try:
        session.clip_now(float(payload.get("seconds") or 30))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return session.state()


@app.post("/api/live/{session_id}/stop")
async def live_stop(session_id: str):
    session = live.stop_session(session_id)
    if session is None:
        raise HTTPException(404, "live session not found")
    return session.state()


# ---------------------------------------------------------------------- settings
def _settings_path() -> Path:
    return config.DATA_DIR / "settings.json"


def _read_settings() -> dict:
    p = _settings_path()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}
    return {}


@app.get("/api/settings")
async def get_settings():
    s = config.llm_settings() or {}
    return {"llm": {
        "base_url": s.get("base_url") or "",
        "model": s.get("model") or "",
        "has_key": bool(s.get("key")),
    }}


@app.put("/api/settings")
async def put_settings(payload: dict):
    """Merge into DATA_DIR/settings.json. llm_key semantics: omitted or ""
    keeps the saved key; explicit null clears it; any other string replaces it."""
    data = _read_settings()
    if payload.get("llm_base_url") is not None:
        data["llm_base_url"] = str(payload["llm_base_url"])
    if payload.get("llm_model") is not None:
        data["llm_model"] = str(payload["llm_model"])
    if "llm_key" in payload:
        key = payload["llm_key"]
        if key is None:
            data["llm_key"] = ""
        elif key != "":
            data["llm_key"] = str(key)
        # "" -> keep existing key
    _settings_path().write_text(json.dumps(data, indent=1), encoding="utf-8")
    return {"ok": True}


def main() -> None:
    for line in assets.ensure_assets():
        print(f"[clipforge] {line}")
    uvicorn.run(app, host=config.HOST, port=config.PORT)
