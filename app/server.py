"""ClipForge HTTP server: JSON API + the no-build web UI.

Local tool, no auth. Settings (LLM endpoint/model/key) live in
DATA_DIR/settings.json as flat keys llm_base_url / llm_model / llm_key —
config.llm_settings() reads them. The API key is never returned by the API.
"""
import json
import shutil
import time
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import assets, brain, captions, config, facecam, jobs, layouts, live, llm, media, presets, preview, publish

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
VERSION = "1.1.0"
UPLOAD_EXTS = {".mp4", ".mov", ".mkv", ".webm"}
STATIC_FILES = {"style.css", "app.js"}

config.ensure_dirs()
store = jobs.JobStore()
store.load_existing()
live.start_watcher(store)

# Last resolved preview metadata per URL (lets /api/preview-frame reuse
# the formats from /api/preview instead of resolving the link twice).
_preview_cache: dict = {}

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


@app.post("/api/caption-preview")
async def caption_preview(payload: dict):
    """One still frame with the chosen caption style burned in (same ASS +
    layout + fonts path as a real render), so the UI can show how captions
    will look before rendering. Pass job_id to preview over that job's
    source frame with its facecam/game regions; pass clip_idx too to use a
    frame from that clip's own moment (the clip editor's live preview)."""
    from fastapi.responses import Response
    frame_path, src_w, src_h, facecam, game = None, 0, 0, None, None
    job_id = str(payload.get("job_id") or "")
    if job_id:
        job = store.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        opts = job.get("options") or {}
        facecam = payload.get("facecam") or opts.get("facecam")
        game = payload.get("game") or opts.get("game")
        clip_idx = payload.get("clip_idx")
        clip = None
        if clip_idx is not None:
            try:
                clip = (job.get("clips") or [])[int(clip_idx)]
            except (IndexError, TypeError, ValueError):
                clip = None
        if clip is not None:
            facecam = payload.get("facecam") or clip.get("facecam") or facecam
            game = payload.get("game") or clip.get("game") or game
            src_str = clip.get("src") or job.get("source_path")
            if src_str and Path(src_str).exists():
                try:
                    pr = media.probe(Path(src_str))
                    src_w, src_h = int(pr.get("width") or 0), int(pr.get("height") or 0)
                    t = max(0.0, float(clip["start"]) - float(clip.get("src_offset") or 0.0) + 0.5)
                    cand = store.dir_for(job_id) / "clips" / f"{clip.get('name') or 'clip'}_edit.jpg"
                    if not cand.is_file():
                        media.frame_at(Path(src_str), t, cand, width=src_w or 960)
                    if cand.is_file():
                        frame_path = cand
                except Exception:  # noqa: BLE001 - fall back to the job frame below
                    frame_path = None
        if frame_path is None:
            cand = store.dir_for(job_id) / "frame.jpg"
            if cand.is_file():
                frame_path = cand
                src_w, src_h = int(job.get("width") or 0), int(job.get("height") or 0)
    try:
        data = preview.render_preview(
            style=payload.get("style"), anim=payload.get("anim"),
            text=payload.get("text"), hook=payload.get("hook"),
            layout=payload.get("layout"), aspect=payload.get("aspect") or "vertical",
            top=payload.get("top"), caption_y=payload.get("caption_y"),
            captions_on=payload.get("captions", True),
            hook_on=payload.get("hook_enabled", True),
            frame_path=frame_path, src_w=src_w, src_h=src_h,
            facecam=facecam, game=game,
            highlight_words=payload.get("highlight_words"),
            caption_custom=payload.get("caption_custom"),
            hook_style=payload.get("hook_style") or "box")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(500, str(e)) from e
    return Response(content=data, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


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


# ---------------------------------------------------------------------- parity additions
@app.post("/api/preview")
async def link_preview(payload: dict):
    """Link preview before a job starts (PeakCut /api/preview): title,
    uploader, duration, thumbnail, is-live — no download. Returns fast;
    the REAL frame (for facecam/gameplay box selection) comes from
    /api/preview-frame, which the UI loads right after and swaps in."""
    url = (payload.get("url") or "").strip()
    if not url or not media.site_allowed(url):
        raise HTTPException(400, "That link is not supported.")
    try:
        m = media.info(url)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, media.friendly_error(e, url)) from e
    _preview_cache[url] = (time.time(), m)
    raw = m.get("raw") or {}
    return {"title": m.get("title") or "", "uploader": m.get("uploader") or "",
            "duration": m.get("duration") or 0, "is_live": bool(m.get("is_live")),
            "width": m.get("width") or 0, "height": m.get("height") or 0,
            "thumbnail": raw.get("thumbnail") or "", "frame": ""}


@app.post("/api/preview-frame")
async def link_preview_frame(payload: dict):
    """One REAL frame from the video (data URI), for drag-selecting the
    facecam/gameplay sections on the actual layout. Separate from
    /api/preview so the metadata shows immediately while the frame
    (a stream fetch, slow on some connections) loads after."""
    url = (payload.get("url") or "").strip()
    if not url or not media.site_allowed(url):
        raise HTTPException(400, "That link is not supported.")
    cached = _preview_cache.get(url)
    m = cached[1] if cached and time.time() - cached[0] < 300 else None
    if m is None:
        try:
            m = media.info(url)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, media.friendly_error(e, url)) from e
    try:
        import base64
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            fp = media.preview_frame(url, Path(td) / "frame.jpg", meta=m)
            frame = "data:image/jpeg;base64," + base64.b64encode(fp.read_bytes()).decode()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, media.friendly_error(e, url)) from e
    return {"frame": frame}


@app.get("/api/readiness")
async def readiness():
    from . import extras
    return extras.readiness()


@app.get("/api/storage")
async def storage_info():
    from . import extras
    return extras.storage_info()


@app.get("/api/update-check")
async def update_check():
    from . import extras
    return extras.update_check()


@app.get("/api/diagnostics")
async def diagnostics():
    """Redacted diagnostics download (NeonClipping 'Report a problem')."""
    from . import extras
    from fastapi.responses import Response
    data = json.dumps(extras.diagnostics(), indent=1, ensure_ascii=False)
    return Response(content=data, media_type="application/json",
                    headers={"Content-Disposition": "attachment; filename=clipforge-diagnostics.json"})


@app.get("/api/profiles")
async def list_profiles():
    from . import extras
    return extras.list_profiles()


@app.post("/api/profiles")
async def save_profile(payload: dict):
    from . import extras
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "give the profile a name")
    return extras.save_profile(name, payload.get("options") or {})


@app.delete("/api/profiles/{name}")
async def delete_profile(name: str):
    from . import extras
    return {"deleted": extras.delete_profile(name)}


@app.get("/api/feedback/summary")
async def feedback_summary():
    from . import learn
    return learn.summary()


@app.post("/api/upload-asset")
async def upload_asset(file: UploadFile = File(...), kind: str = Form("image")):
    """Upload a logo/watermark image or a music track (NeonClipping
    /upload-asset). Returns the server path to put in options
    (watermark.image / music.path)."""
    ext = Path(file.filename or "").suffix.lower()
    if kind == "image" and ext not in (".png", ".jpg", ".jpeg", ".webp"):
        raise HTTPException(400, "logo must be a PNG, JPG or WebP image")
    if kind == "audio" and ext not in (".mp3", ".wav", ".m4a", ".ogg", ".flac"):
        raise HTTPException(400, "music must be MP3, WAV, M4A, OGG or FLAC")
    if kind not in ("image", "audio"):
        raise HTTPException(400, "kind must be image or audio")
    assets_dir = config.DATA_DIR / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    dest = assets_dir / f"{uuid.uuid4().hex}{ext}"
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    if kind == "audio":
        try:
            media.probe(dest)
        except Exception as e:  # noqa: BLE001
            dest.unlink(missing_ok=True)
            raise HTTPException(400, f"that audio file could not be read: {e}") from e
    return {"path": str(dest), "name": file.filename or dest.name, "kind": kind}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    try:
        return jobs.cancel_job(job, store)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/jobs/{job_id}/review-select")
async def review_select(job_id: str, payload: dict):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    try:
        return jobs.resume_with_selection(job, store, payload.get("indices") or [])
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/jobs/{job_id}/manual-clip")
async def manual_clip(job_id: str, payload: dict):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    try:
        return jobs.manual_clip(job, store, payload.get("start"), payload.get("end"),
                                title=str(payload.get("title") or ""),
                                hook=str(payload.get("hook") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/jobs/{job_id}/transcript")
async def transcript(job_id: str, q: str = ""):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return jobs.transcript_lines(job, store, q)


@app.get("/api/jobs/{job_id}/clips/{idx}/words")
async def get_clip_words(job_id: str, idx: int):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    try:
        return {"words": jobs.get_clip_words(job, store, idx)}
    except IndexError as e:
        raise HTTPException(404, str(e)) from e


@app.post("/api/jobs/{job_id}/clips/{idx}/words")
async def set_clip_words(job_id: str, idx: int, payload: dict):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    try:
        clip = jobs.set_clip_words(job, store, idx, payload.get("words") or [])
        return {"ok": True, "words": len(clip.get("words_override") or [])}
    except IndexError as e:
        raise HTTPException(404, str(e)) from e


@app.post("/api/jobs/{job_id}/clips/{idx}/subtitles")
async def import_subtitles(job_id: str, idx: int, payload: dict):
    """Import SRT/VTT/ASS text as this clip's caption words (times are
    clip-relative, like a subtitle file made for the clip itself)."""
    from . import subtitles
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    try:
        words = subtitles.words_from_text(str(payload.get("text") or ""),
                                          payload.get("format"))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    try:
        clip = jobs.set_clip_words(job, store, idx, words)
        return {"ok": True, "words": len(clip.get("words_override") or [])}
    except IndexError as e:
        raise HTTPException(404, str(e)) from e


@app.put("/api/jobs/{job_id}/clips/{idx}/text")
async def clip_text(job_id: str, idx: int, payload: dict):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    try:
        return jobs.update_clip_text(job, store, idx, payload)
    except IndexError as e:
        raise HTTPException(404, str(e)) from e


@app.post("/api/jobs/{job_id}/clips/{idx}/write")
async def clip_write(job_id: str, idx: int):
    """AI rewrite of the post text (PeakCut /write): fresh title/hook/
    description/hashtags from the clip's own words."""
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    try:
        words = jobs.get_clip_words(job, store, idx)
    except IndexError as e:
        raise HTTPException(404, str(e)) from e
    desc = {}
    try:
        desc = brain.describe(words, fallback_title=job.get("title") or "")
    except Exception:  # noqa: BLE001
        desc = {}
    if not desc:
        raise HTTPException(400, "could not write post text for this clip")
    fields = {k: desc[k] for k in ("title", "hook", "description", "hashtags") if desc.get(k)}
    return jobs.update_clip_text(job, store, idx, fields)


@app.post("/api/jobs/{job_id}/clips/{idx}/rating")
async def clip_rating(job_id: str, idx: int, payload: dict):
    from . import learn
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    clips = job.get("clips") or []
    if idx < 0 or idx >= len(clips):
        raise HTTPException(404, "clip not found")
    clip = clips[idx]
    try:
        rating = float(payload.get("rating"))
    except (TypeError, ValueError):
        raise HTTPException(400, "rating must be 1-5")
    clip["rating"] = max(1, min(5, int(round(rating))))
    store.save(job)
    learn.record_rating(job_id, idx, clip["rating"], clip.get("features") or {},
                        layout=str(clip.get("layout") or ""),
                        style=str((job.get("options") or {}).get("style") or ""))
    return {"ok": True, "rating": clip["rating"], "summary": learn.summary()}


@app.get("/api/llm/providers")
async def llm_providers():
    return llm.PROVIDERS


@app.post("/api/llm/models")
async def llm_models(payload: dict):
    key = str(payload.get("key") or "")
    if not key:
        key = (config.llm_settings() or {}).get("key") or ""
    return {"models": llm.list_models(payload.get("base_url") or "", key)}


@app.post("/api/llm/test")
async def llm_test(payload: dict):
    key = str(payload.get("key") or "")
    if not key:
        key = (config.llm_settings() or {}).get("key") or ""
    return llm.test(payload.get("base_url") or "", payload.get("model") or "", key)


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
    raw = _read_settings()
    return {"llm": {
        "base_url": s.get("base_url") or "",
        "model": s.get("model") or "",
        "has_key": bool(s.get("key")),
    },
        "discord_webhook": raw.get("discord_webhook") or "",
        "telegram_chat_id": raw.get("telegram_chat_id") or "",
        "has_telegram_token": bool(raw.get("telegram_token")),
        "has_phone_pin": bool(raw.get("phone_pin")),
        "auto_record": bool(raw.get("auto_record")),
    }


@app.put("/api/settings")
async def put_settings(payload: dict):
    """Merge into DATA_DIR/settings.json. llm_key semantics: omitted or ""
    keeps the saved key; explicit null clears it; any other string replaces it.
    Same semantics for telegram_token and phone_pin."""
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
    if payload.get("discord_webhook") is not None:
        data["discord_webhook"] = str(payload["discord_webhook"])[:500]
    if payload.get("telegram_chat_id") is not None:
        data["telegram_chat_id"] = str(payload["telegram_chat_id"])[:50]
    if "telegram_token" in payload:
        tok = payload["telegram_token"]
        if tok is None:
            data["telegram_token"] = ""
        elif tok != "":
            data["telegram_token"] = str(tok)
    if "phone_pin" in payload:
        pin = payload["phone_pin"]
        if pin is None:
            data["phone_pin"] = ""
        elif pin != "":
            data["phone_pin"] = str(pin)[:12]
            if not data.get("phone_secret"):
                data["phone_secret"] = uuid.uuid4().hex
    if "auto_record" in payload:
        data["auto_record"] = bool(payload["auto_record"])
    _settings_path().write_text(json.dumps(data, indent=1), encoding="utf-8")
    return {"ok": True}


# ---------------------------------------------------------------------- phone remote
def _phone_cookie_value(data: dict) -> str:
    import hashlib
    import hmac
    secret = str(data.get("phone_secret") or "")
    pin = str(data.get("phone_pin") or "")
    return hmac.new(secret.encode(), pin.encode(), hashlib.sha256).hexdigest()


_PHONE_ALLOWED = ("/api/status", "/api/jobs", "/api/live", "/api/phone")


@app.middleware("http")
async def _phone_gate(request: Request, call_next):
    """NeonClipping phone remote gate: when a phone PIN is set, non-local
    API requests need the PIN cookie and only reach allowlisted routes.
    Local (loopback) requests always pass. No PIN set = no gate."""
    data = _read_settings()
    pin = str(data.get("phone_pin") or "")
    if pin:
        host = (request.client.host if request.client else "") or ""
        local = host in ("127.0.0.1", "::1", "localhost", "testclient")
        path = request.url.path
        if not local and path.startswith("/api/"):
            ok_cookie = request.cookies.get("cf_phone") == _phone_cookie_value(data)
            allowed = any(path == p or path.startswith(p + "/") for p in _PHONE_ALLOWED)
            if not (ok_cookie and allowed):
                return JSONResponse({"error": "phone PIN required"}, status_code=401)
    return await call_next(request)


@app.get("/m")
async def phone_page():
    return FileResponse(WEB_DIR / "mobile.html")


@app.get("/api/phone/status")
async def phone_status(request: Request):
    data = _read_settings()
    pin_set = bool(data.get("phone_pin"))
    authed = (not pin_set) or request.cookies.get("cf_phone") == _phone_cookie_value(data)
    return {"pin_set": pin_set, "authed": authed}


@app.post("/api/phone/login")
async def phone_login(payload: dict):
    import hmac as _hmac
    data = _read_settings()
    pin = str(data.get("phone_pin") or "")
    if not pin:
        return {"ok": True, "pin_set": False}
    if not _hmac.compare_digest(str(payload.get("pin") or ""), pin):
        raise HTTPException(401, "wrong PIN")
    resp = JSONResponse({"ok": True, "pin_set": True})
    resp.set_cookie("cf_phone", _phone_cookie_value(data), httponly=True,
                    samesite="lax", max_age=30 * 24 * 3600)
    return resp


def main() -> None:
    for line in assets.ensure_assets():
        print(f"[clipforge] {line}")
    uvicorn.run(app, host=config.HOST, port=config.PORT)
