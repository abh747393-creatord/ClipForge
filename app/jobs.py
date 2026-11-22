"""ClipForge job store + video pipeline.

A job turns one source (URL or uploaded file) into captioned clips:

  ingest -> transcript (YouTube captions or Whisper) -> signal scan ->
  clip finding (brain) -> per-clip render

Two ingest paths:
  * FAST PATH (URL longer than 600 s): download audio only, get words from
    YouTube captions when available, scan audio-only, then download just the
    chosen sections and line them up with find_offset.
  * WHOLE PATH (short URL or local file): download/open the whole video and
    cut every clip from it.

Render contract note: render.render_clip expects `words_abs` on the timeline
of the SOURCE FILE it is given. On the whole path that is just the job's
absolute words. On the fast path the words are shifted by the section offset
(section_time = absolute_time - offset) before rendering. Each clip records
"src" and "src_offset" so rerender_clip can rebuild the same shift later.
"""
import copy
import json
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import brain, captions, captions_yt, config, facecam, gameevents, layouts, media, presets, publish, render, roman, signals, transcribe

DEFAULT_OPTIONS = {
    "count": 4,
    "min_len": 15,
    "max_len": 60,
    "layout": "stack",
    "style": "bounce",
    "anim": "highlight",
    "aspect": "vertical",
    "resolution": 1080,
    "fps": 0,
    "language": None,
    "hinglish": False,
    "use_llm": True,
    "captions": True,
    "hook_enabled": True,
    "facecam": None,
    "game": None,
    "top": 0.32,
    "caption_y": 1450,
    "watermark": {"text": None, "image": None, "position": "br", "opacity": 0.85},
    "music": {"path": None, "volume": 0.15},
    "template": None,
    "quality": 1080,
    "live_before": 24,
    "live_after": 14,
    "ai_every_min": 3,
    # stream-clips + Wanted additions
    "min_score": 0,            # drop clips scoring below this (0 = keep all)
    "focus": "",               # user brief for the AI ("only clutch rounds")
    "preset": None,            # style preset id (presets.py)
    "preset_filter": "",       # ffmpeg filter chain from the preset (internal)
    "title_bar": False,        # Pillow title bar (emoji) instead of ASS hook box
    "killcam": False,          # slow-mo replay of the peak moment appended
    "gta_overlay": {"stars": None, "mission_card": False},
    "campaign": {"handle": "", "tag": "#ad"},  # paid-campaign banner burned in
    "scan_model": None,        # whisper model for the whole-video scan (None = config)
    "caption_model": None,     # whisper model for per-clip captions (None = same as scan)
    "brain_mode": "auto",      # auto | llm | manual (manual = prompts for your own AI)
    "game_events": True,       # watch the game for WASTED/explosions/crashes
}

FAST_PATH_MIN_DURATION = 600  # seconds; longer URLs use the audio-first path
SECTION_PAD_BEFORE = 4        # extra seconds downloaded before a fast-path clip
SECTION_PAD_AFTER = 1         # extra seconds downloaded after a fast-path clip

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="clipforge-job")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp(value, lo, hi, fallback):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(lo, min(hi, v))


def _clean_rect(rect):
    """None, or {x,y,w,h} floats clamped to 0..1 with w/h > 0.02. Accepts dict or [x,y,w,h]."""
    if rect is None:
        return None
    try:
        if isinstance(rect, dict):
            x, y, w, h = rect["x"], rect["y"], rect["w"], rect["h"]
        else:
            x, y, w, h = rect[0], rect[1], rect[2], rect[3]
        x, y = _clamp(x, 0.0, 1.0, 0.0), _clamp(y, 0.0, 1.0, 0.0)
        w, h = _clamp(w, 0.0, 1.0, 0.0), _clamp(h, 0.0, 1.0, 0.0)
    except (KeyError, IndexError, TypeError):
        return None
    if w <= 0.02 or h <= 0.02:
        return None
    # keep the box inside the frame
    w = min(w, 1.0 - x) if x < 1.0 else w
    h = min(h, 1.0 - y) if y < 1.0 else h
    if w <= 0.02 or h <= 0.02:
        return None
    return {"x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4)}


def _aspect_ids():
    aspects = getattr(layouts, "ASPECTS", None)
    if isinstance(aspects, dict):
        return set(aspects.keys())
    if isinstance(aspects, (list, tuple)):
        ids = set()
        for a in aspects:
            ids.add(a[0] if isinstance(a, (list, tuple)) else a)
        return ids
    return {"vertical", "square", "portrait", "landscape"}


def clean_options(raw: dict | None) -> dict:
    """DEFAULT_OPTIONS overlaid with `raw`, every known key validated/clamped.
    Unknown keys (e.g. live_per_hour) pass through untouched. A style preset
    (presets.py) fills in values the caller did not set explicitly."""
    if isinstance(raw, dict):
        raw = presets.apply(raw)
    opts = copy.deepcopy(DEFAULT_OPTIONS)
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in ("watermark", "music", "gta_overlay", "campaign") and isinstance(v, dict):
                merged = dict(opts.get(k) or {})
                merged.update(v)
                opts[k] = merged
            else:
                opts[k] = v

    opts["count"] = int(_clamp(opts.get("count"), 1, 12, DEFAULT_OPTIONS["count"]))
    opts["min_len"] = _clamp(opts.get("min_len"), 5, 120, DEFAULT_OPTIONS["min_len"])
    opts["max_len"] = _clamp(opts.get("max_len"), 10, 180, DEFAULT_OPTIONS["max_len"])
    if opts["max_len"] <= opts["min_len"]:
        opts["max_len"] = min(180.0, opts["min_len"] + 5)
    # keep whole seconds when the caller sent whole seconds
    for k in ("min_len", "max_len"):
        if float(opts[k]).is_integer():
            opts[k] = int(opts[k])

    layout_ids = {c[0] for c in layouts.CATALOG}
    if opts.get("layout") not in layout_ids:
        opts["layout"] = "stack"
    if opts.get("style") not in captions.STYLES:
        opts["style"] = "bounce"
    if opts.get("anim") not in captions.ANIMS:
        opts["anim"] = "highlight"
    if opts.get("aspect") not in _aspect_ids():
        opts["aspect"] = "vertical"
    if opts.get("resolution") not in (720, 1080, 2160):
        opts["resolution"] = 1080
    if opts.get("fps") not in (0, 24, 30, 60):
        opts["fps"] = 0
    if opts.get("quality") not in (360, 480, 720, 1080, 1440, 2160):
        opts["quality"] = 1080
    opts["top"] = _clamp(opts.get("top"), 0.2, 0.5, DEFAULT_OPTIONS["top"])
    opts["caption_y"] = int(_clamp(opts.get("caption_y"), 0, 4000, DEFAULT_OPTIONS["caption_y"]))

    opts["facecam"] = _clean_rect(opts.get("facecam"))
    opts["game"] = _clean_rect(opts.get("game"))

    wm = dict(DEFAULT_OPTIONS["watermark"])
    if isinstance(opts.get("watermark"), dict):
        wm.update(opts["watermark"])
    wm["opacity"] = _clamp(wm.get("opacity"), 0.0, 1.0, 0.85)
    if wm.get("position") not in ("tl", "tr", "bl", "br"):
        wm["position"] = "br"
    opts["watermark"] = wm

    mu = dict(DEFAULT_OPTIONS["music"])
    if isinstance(opts.get("music"), dict):
        mu.update(opts["music"])
    mu["volume"] = _clamp(mu.get("volume"), 0.0, 1.0, 0.15)
    opts["music"] = mu

    if opts.get("template") not in (None, "news"):
        opts["template"] = None
    lang = opts.get("language")
    opts["language"] = lang if isinstance(lang, str) and lang else None
    for k in ("hinglish", "use_llm", "captions", "hook_enabled",
              "title_bar", "killcam", "game_events"):
        opts[k] = bool(opts.get(k, DEFAULT_OPTIONS[k]))
    opts["min_score"] = _clamp(opts.get("min_score"), 0, 100, 0)
    opts["focus"] = str(opts.get("focus") or "")[:300]
    if opts.get("preset") not in presets.PRESETS:
        opts["preset"] = None
    opts["preset_filter"] = str(opts.get("preset_filter") or "")[:300]
    gta = dict(DEFAULT_OPTIONS["gta_overlay"])
    if isinstance(opts.get("gta_overlay"), dict):
        gta.update(opts["gta_overlay"])
    if gta.get("stars") not in (None, "auto", 1, 2, 3, 4, 5):
        try:
            gta["stars"] = int(gta["stars"]) if 1 <= int(gta["stars"]) <= 5 else None
        except (TypeError, ValueError):
            gta["stars"] = None
    gta["mission_card"] = bool(gta.get("mission_card"))
    opts["gta_overlay"] = gta
    camp = dict(DEFAULT_OPTIONS["campaign"])
    if isinstance(opts.get("campaign"), dict):
        camp.update(opts["campaign"])
    camp["handle"] = str(camp.get("handle") or "")[:40]
    camp["tag"] = str(camp.get("tag") or "#ad")[:20]
    opts["campaign"] = camp
    for k in ("scan_model", "caption_model"):
        v = opts.get(k)
        opts[k] = v if isinstance(v, str) and v.strip() else None
    if opts.get("brain_mode") not in ("auto", "llm", "manual"):
        opts["brain_mode"] = "auto"
    if opts["brain_mode"] == "llm":
        opts["use_llm"] = True
    opts["live_before"] = int(_clamp(opts.get("live_before"), 5, 120, DEFAULT_OPTIONS["live_before"]))
    opts["live_after"] = int(_clamp(opts.get("live_after"), 3, 60, DEFAULT_OPTIONS["live_after"]))
    opts["ai_every_min"] = int(_clamp(opts.get("ai_every_min"), 1, 30, DEFAULT_OPTIONS["ai_every_min"]))
    if "live_per_hour" in opts:
        opts["live_per_hour"] = int(_clamp(opts.get("live_per_hour"), 1, 20, 4))
    return opts


class JobStore:
    """Jobs live as JSON files: DATA_DIR/jobs/<id>/job.json (+ clips/ subdir)."""

    def __init__(self, root=None):
        self.root = Path(root) if root else config.DATA_DIR / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def dir_for(self, job_id: str) -> Path:
        return self.root / job_id

    def create(self, source: dict, options: dict | None) -> dict:
        job_id = uuid.uuid4().hex[:12]
        d = self.dir_for(job_id)
        (d / "clips").mkdir(parents=True, exist_ok=True)
        source = {
            "type": source.get("type") or "url",
            "url": source.get("url"),
            "path": source.get("path"),
            "name": source.get("name") or source.get("url") or source.get("path") or "",
        }
        job = {
            "id": job_id,
            "kind": "live" if source["type"] == "live" else "video",
            "status": "queued",
            "stage": "queued",
            "progress": 0.0,
            "created": _now_iso(),
            "source": source,
            "title": "",
            "uploader": "",
            "duration": 0,
            "width": 0,
            "height": 0,
            "options": clean_options(options),
            "clips": [],
            "error": None,
            "notes": [],
            "transcript_from": "none",
            "source_path": None,
        }
        self.save(job)
        return job

    def save(self, job: dict) -> bool:
        """Persist job.json (atomic via tmp+replace). Best-effort: a transient
        write failure (e.g. disk quota flake) must never kill a running job —
        the in-memory job keeps going and the next save usually succeeds.
        Returns True when the file was written."""
        d = self.dir_for(job["id"])
        tmp = d / f"job.json.tmp{uuid.uuid4().hex[:8]}"
        try:
            d.mkdir(parents=True, exist_ok=True)
            with self._lock:
                tmp.write_text(json.dumps(job, indent=1, ensure_ascii=False), encoding="utf-8")
                tmp.replace(d / "job.json")
            return True
        except OSError as e:
            print(f"[clipforge] warning: could not save job {job.get('id')}: {e}", flush=True)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def get(self, job_id: str) -> dict | None:
        if not job_id or not str(job_id).isalnum():
            return None
        p = self.dir_for(str(job_id)) / "job.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def list(self) -> list:
        jobs = []
        for p in self.root.glob("*/job.json"):
            try:
                jobs.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        jobs.sort(key=lambda j: j.get("created", ""), reverse=True)
        return jobs

    def delete(self, job_id: str) -> bool:
        if not job_id or not str(job_id).isalnum():
            return False
        d = self.dir_for(str(job_id))
        if not d.exists():
            return False
        shutil.rmtree(d, ignore_errors=True)
        return not d.exists()

    def load_existing(self) -> list:
        """Startup recovery: anything left queued/running by a previous process
        is marked as an error so it does not sit 'running' forever."""
        jobs = self.list()
        for job in jobs:
            if job.get("status") in ("queued", "running"):
                job["status"] = "error"
                job["error"] = "interrupted by restart"
                job["stage"] = "error"
                self.save(job)
        return jobs


def job_dir(store: JobStore, job: dict) -> Path:
    return store.dir_for(job["id"])


def _set(job: dict, store: JobStore, **kw) -> None:
    job.update(kw)
    store.save(job)


def _note(job: dict, store: JobStore, text: str) -> None:
    job.setdefault("notes", []).append(text)
    store.save(job)


def _noop_report(*_args, **_kwargs):
    """Progress callback sink for sibling modules (their report signatures vary)."""
    return None


def _yt_words(meta):
    """YouTube captions for a media.info() result, or [] when there are none.
    captions_yt.from_info raises on network/parse failures — that means
    'no captions', never a failed job."""
    if not meta:
        return []
    try:
        return captions_yt.from_info(meta.get("raw") or meta) or []
    except Exception:  # noqa: BLE001
        return []


def _language_for(options: dict):
    """Language passed to Whisper. Hinglish mode asks Whisper for 'hinglish'
    (roman-script Hindi) when no explicit language was chosen."""
    lang = options.get("language")
    if options.get("hinglish") and not lang:
        return "hinglish"
    return lang


def _hinglish_wanted(options: dict) -> bool:
    return bool(options.get("hinglish")) or options.get("language") == "hinglish"


def _shift_words(words, offset: float, span_end: float | None = None):
    """Words on the absolute timeline -> words on a section file's timeline
    (section_time = absolute_time - offset). Words overlapping the section are
    kept, with starts clamped at 0."""
    out = []
    for w in words or []:
        s, e = float(w["start"]) - offset, float(w["end"]) - offset
        if e <= 0:
            continue
        if span_end is not None and s >= span_end:
            continue
        out.append({"start": max(0.0, s), "end": max(0.0, e), "text": w["text"]})
    return out


def _basename_fields(result: dict) -> dict:
    """Render results may carry full paths; job.json stores basenames so the
    /media routes and export stay simple (files live in the clips dir)."""
    out = dict(result or {})
    for k in ("file", "cover", "ass", "srt", "vtt", "meta", "check"):
        v = out.get(k)
        if isinstance(v, str) and v:
            out[k] = Path(v).name
    return out


def _prepare_clip(clip: dict, options: dict) -> dict:
    clip = dict(clip)
    for k, v in (("title", ""), ("hook", ""), ("description", ""), ("caption", ""),
                 ("hashtags", []), ("highlight_words", []), ("reason", ""), ("why", ""),
                 ("kind", ""), ("game_event", ""), ("look", ""), ("features", {}),
                 ("score", 0), ("status", "pending"), ("error", None)):
        clip.setdefault(k, v)
    clip.setdefault("viral_score", clip.get("score", 0))
    clip.setdefault("facecam", options.get("facecam"))
    clip.setdefault("game", options.get("game"))
    return clip


def _cam_memory_key(source: dict) -> str:
    import hashlib
    raw = source.get("url") or source.get("path") or source.get("name") or ""
    return hashlib.sha1(str(raw).encode("utf-8")).hexdigest()[:16]


def _cam_memory_get(source: dict):
    """Facecam box remembered for this source from an earlier job (stream-clips
    cam.json idea), or None."""
    try:
        data = json.loads((config.DATA_DIR / "cam_memory.json").read_text(encoding="utf-8"))
        rect = data.get(_cam_memory_key(source)) if isinstance(data, dict) else None
        return _clean_rect(rect)
    except (OSError, ValueError):
        return None


def _cam_memory_set(source: dict, rect: dict) -> None:
    try:
        p = config.DATA_DIR / "cam_memory.json"
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data[_cam_memory_key(source)] = rect
        p.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def submit(job: dict, store: JobStore) -> None:
    """Run the job on the shared 2-worker pool. run_job handles its own errors;
    this wrapper is the backstop so a job can never die silently."""

    def _run():
        try:
            run_job(job, store)
        except Exception as e:  # noqa: BLE001 - last-resort guard
            try:
                job["status"] = "error"
                job["stage"] = "error"
                job["error"] = str(e)
                store.save(job)
            except Exception:  # noqa: BLE001
                pass

    _executor.submit(_run)


def _find_or_park(job, store, jdir, words, scan, duration, options):
    """brain.find_clips — or, in manual brain mode, park the job: write the
    transcript prompts (stream-clips 'agent' brain) and wait for the user to
    submit picks from any AI. Returns clips, or None when parked."""
    if options.get("brain_mode") == "manual" and not job.get("manual_picks"):
        for name, data in (("scan.json", scan), ("words.json", words)):
            try:
                (jdir / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        prompts = brain.build_prompts(words, scan, duration, options)
        pdir = jdir / "prompts"
        pdir.mkdir(parents=True, exist_ok=True)
        for p in prompts:
            try:
                (pdir / f"part_{p['part']:04d}.txt").write_text(p["prompt"], encoding="utf-8")
            except OSError:
                pass
        job["prompts"] = [{"part": p["part"], "start": p["start"], "end": p["end"]} for p in prompts]
        _set(job, store, status="waiting_ai", stage="waiting_ai", progress=0.65)
        _note(job, store, "manual AI mode: copy a prompt into any AI, then paste its JSON picks back here")
        return None
    return brain.find_clips(words, scan, duration, options, report=_noop_report,
                            manual_picks=job.get("manual_picks"))


def run_job(job: dict, store: JobStore) -> None:
    jdir = job_dir(store, job)
    clips_dir = jdir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    options = job["options"]
    source = job["source"]
    url = source.get("url")
    is_file = source.get("type") == "file"

    try:
        _set(job, store, status="running", stage="downloading", progress=0.1, error=None)

        meta = None
        duration = 0.0
        words: list = []
        scan = None
        fast = False
        src = None            # whole-path source file
        src_w = src_h = 0
        wav16 = None          # 16 kHz full-audio wav (fast path offset matching)
        audio = None          # downloaded audio (fast path)
        clips = None

        # Resume after manual AI picks: words + scan were persisted when the
        # job parked, so ingest/transcribe/scan are skipped entirely.
        resume = bool(job.get("manual_picks")) and (jdir / "words.json").exists() \
            and (jdir / "scan.json").exists()
        if resume:
            words = json.loads((jdir / "words.json").read_text(encoding="utf-8"))
            scan = json.loads((jdir / "scan.json").read_text(encoding="utf-8"))
            duration = float(job.get("duration") or 0)
            src_w, src_h = int(job.get("width") or 0), int(job.get("height") or 0)
            fast = bool(job.get("_fast"))
            if job.get("source_path"):
                src = Path(job["source_path"])
            if fast:
                cands = sorted(jdir.glob("audio_src.*"))
                audio = cands[0] if cands else None
                if audio is None:
                    raise RuntimeError("the downloaded audio for this job is gone")
            _set(job, store, stage="finding", progress=0.65)
            clips = _find_or_park(job, store, jdir, words, scan, duration, options)
        elif is_file:
            src = Path(source["path"])
            if not src.exists():
                raise RuntimeError("The uploaded file is missing.")
            pr = media.probe(src)
            duration = float(pr.get("duration") or 0)
            src_w, src_h = int(pr.get("width") or 0), int(pr.get("height") or 0)
            job["source_path"] = str(src)
            _set(job, store, duration=duration, width=src_w, height=src_h,
                 title=job.get("title") or source.get("name") or "")
        else:
            meta = media.info(url)
            if meta.get("is_live"):
                raise RuntimeError("That link is live — use the Live tab")
            duration = float(meta.get("duration") or 0)
            _set(job, store, title=meta.get("title") or "", uploader=meta.get("uploader") or "",
                 duration=duration, width=int(meta.get("width") or 0), height=int(meta.get("height") or 0))
            fast = duration > FAST_PATH_MIN_DURATION
            job["_fast"] = fast

        if resume:
            pass  # clips already found above
        elif fast:
            # ---------- FAST PATH: audio first, sections later ----------
            audio = media.download_audio(url, jdir, progress_cb=_noop_report)
            _set(job, store, stage="transcribing", progress=0.35)
            words = _yt_words(meta)
            if words:
                job["transcript_from"] = "youtube"
                if _hinglish_wanted(options):
                    words = roman.words(words)
            else:
                wav16 = media.extract_audio(audio, jdir / "audio16.wav")
                res = transcribe.transcribe(audio, 0, duration, jdir,
                                            language=_language_for(options),
                                            model=options.get("scan_model"), report=_noop_report)
                words = res.get("words") or []  # start=0, so already absolute
                job["transcript_from"] = "whisper"
            _set(job, store, stage="scanning", progress=0.55)
            chat_rate = media.chat_speed(url, meta, jdir)
            scan = signals.scan(audio, duration, words=words, motion_on=False,
                                report=_noop_report, chat_rate=chat_rate)
            _note(job, store, "motion + game-event scan skipped in fast mode (audio only)")
            _set(job, store, stage="finding", progress=0.65)
            clips = _find_or_park(job, store, jdir, words, scan, duration, options)
        else:
            # ---------- WHOLE PATH: one source file for every clip ----------
            if not is_file:
                src = media.download(url, jdir, height=int(options.get("quality") or 1080),
                                     progress_cb=_noop_report)
                pr = media.probe(src)
                duration = float(pr.get("duration") or duration)
                src_w, src_h = int(pr.get("width") or 0), int(pr.get("height") or 0)
                job["source_path"] = str(src)
                _set(job, store, duration=duration, width=src_w, height=src_h)
            _set(job, store, stage="transcribing", progress=0.35)
            if not is_file:
                words = _yt_words(meta)
            if words:
                job["transcript_from"] = "youtube"
                if _hinglish_wanted(options):
                    words = roman.words(words)
            else:
                res = transcribe.transcribe(src, 0, duration, jdir,
                                            language=_language_for(options),
                                            model=options.get("scan_model"), report=_noop_report)
                words = res.get("words") or []
                job["transcript_from"] = "whisper"
            _set(job, store, stage="scanning", progress=0.55)
            game = None
            if options.get("game_events", True):
                game = gameevents.scan(src, duration, report=_noop_report)
                if game.get("events"):
                    job["game_events"] = game["events"]
                    _note(job, store, f"game events found: "
                                      + ", ".join(sorted({e['type'] for e in game['events']})))
            chat_rate = media.chat_speed(url, meta, jdir) if not is_file else None
            scan = signals.scan(src, duration, words=words, motion_on=True,
                                report=_noop_report, chat_rate=chat_rate, game=game)
            _set(job, store, stage="finding", progress=0.65)
            clips = _find_or_park(job, store, jdir, words, scan, duration, options)

        if clips is None:
            return  # parked in manual AI mode; resume via /ai-answers

        try:
            (jdir / "words.json").write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
        except OSError as e:  # quota flake: rerenders lose words, but the job itself must go on
            _note(job, store, f"could not save transcript cache: {e}")
        clips = [_prepare_clip(c, options) for c in (clips or [])]
        job["clips"] = clips
        store.save(job)
        if not clips:
            _note(job, store, "no clips were found in this video")

        # ---------- per-clip source resolution (fast path downloads sections) ----------
        frame_src = src
        frame_t = min(5.0, duration * 0.1) if duration else 0.0
        detect_src, detect_dur, detect_w, detect_h = src, duration, src_w, src_h

        if fast:
            if wav16 is None:
                wav16 = media.extract_audio(audio, jdir / "audio16.wav")
            for i, clip in enumerate(clips):
                start, end = float(clip["start"]), float(clip["end"])
                sec_start = max(0.0, start - SECTION_PAD_BEFORE)
                section = media.download_section(url, jdir, sec_start, end + SECTION_PAD_AFTER,
                                                 height=int(options.get("quality") or 1080))
                pr = media.probe(section)
                sec_dur = float(pr.get("duration") or 0)
                clip_w, clip_h = int(pr.get("width") or 0), int(pr.get("height") or 0)
                try:
                    sec16 = media.extract_audio(section, jdir / f"section{i + 1:02d}_16.wav")
                    offset = float(media.find_offset(wav16, sec16, approx_start=sec_start))
                except Exception:  # noqa: BLE001 - fall back to the requested start
                    offset = sec_start
                clip["src"] = str(section)
                clip["src_offset"] = offset
                clip["_sec_dur"] = sec_dur
                clip["_src_w"], clip["_src_h"] = clip_w, clip_h
                if i == 0:
                    frame_src = section
                    frame_t = min(5.0, sec_dur * 0.1) if sec_dur else 0.0
                    detect_src, detect_dur, detect_w, detect_h = section, sec_dur, clip_w, clip_h
            store.save(job)
        else:
            for clip in clips:
                clip["src"] = str(src)
                clip["src_offset"] = 0.0
                clip["_sec_dur"] = duration
                clip["_src_w"], clip["_src_h"] = src_w, src_h
            store.save(job)

        # ---------- preview frame (for the region editor) ----------
        try:
            if frame_src is not None:
                media.frame_at(frame_src, frame_t, jdir / "frame.jpg")
        except Exception as e:  # noqa: BLE001 - a missing frame only loses the editor preview
            _note(job, store, f"preview frame failed: {e}")

        # ---------- facecam auto-detect (once; rect is normalized, transfers across sections) ----------
        if layouts.needs_cam(options.get("layout")) and not options.get("facecam") and detect_src is not None:
            try:
                remembered = _cam_memory_get(source)
                if remembered:
                    options["facecam"] = remembered
                    job["options"] = options
                    for clip in clips:
                        clip["facecam"] = remembered
                    store.save(job)
                    _note(job, store, "facecam reused from an earlier job on this source")
                else:
                    det = facecam.detect(detect_src, detect_dur, detect_w, detect_h)
                    if det:
                        options["facecam"] = {"x": det["x"], "y": det["y"], "w": det["w"], "h": det["h"]}
                        job["options"] = options
                        for clip in clips:
                            clip["facecam"] = options["facecam"]
                        _cam_memory_set(source, options["facecam"])
                        store.save(job)
            except Exception as e:  # noqa: BLE001 - detection is best-effort
                _note(job, store, f"facecam detection failed: {e}")

        # ---------- render ----------
        _set(job, store, stage="rendering", progress=0.7)
        n = max(1, len(clips))
        for i, clip in enumerate(clips):
            _set(job, store, progress=0.7 + 0.3 * (i / n))
            name = f"clip{i + 1:02d}"
            clip["name"] = name
            offset = float(clip.get("src_offset") or 0.0)
            clip_src = Path(clip["src"])
            sec_dur = float(clip.pop("_sec_dur", 0) or 0)
            cw = int(clip.pop("_src_w", 0) or 0)
            ch = int(clip.pop("_src_h", 0) or 0)
            try:
                # render_clip wants words on the SOURCE FILE's timeline:
                # whole path -> absolute words; fast path -> shifted by the section offset.
                words_for_render = words if offset == 0.0 else _shift_words(words, offset, sec_dur or None)
                # Two-model captions (stream-clips): the scan used a fast model;
                # re-transcribe just this clip with the accurate one for captions.
                cap_model = options.get("caption_model")
                if cap_model and job.get("transcript_from") == "whisper":
                    try:
                        rel_start = float(clip["start"]) - offset
                        rel_end = float(clip["end"]) - offset
                        res2 = transcribe.transcribe(clip_src, rel_start, rel_end, jdir,
                                                     language=_language_for(options),
                                                     model=cap_model, report=_noop_report)
                        w2 = [dict(w, start=w["start"] + rel_start, end=w["end"] + rel_start)
                              for w in (res2.get("words") or [])]
                        if w2:
                            words_for_render = w2
                    except Exception:  # noqa: BLE001 - keep the scan transcript
                        pass
                clip_for_render = dict(clip, start=float(clip["start"]) - offset,
                                       end=float(clip["end"]) - offset)
                result = render.render_clip(clip_src, clips_dir, name, clip_for_render,
                                            words_for_render, cw, ch, options)
                clip.update(_basename_fields(result))
                clip["status"] = "done"
                clip["error"] = None
            except Exception as e:  # noqa: BLE001 - one bad clip must not kill the job
                clip["status"] = "error"
                clip["error"] = str(e)
            store.save(job)

        try:
            publish.posting_sheet(job, clips_dir)
        except Exception:  # noqa: BLE001 - the sheet is a bonus export
            pass
        _set(job, store, status="done", stage="done", progress=1.0)
    except Exception as e:  # noqa: BLE001
        _set(job, store, status="error", stage="error", error=media.friendly_error(e, url or ""))


def resume_with_picks(job: dict, store: JobStore, picks: list) -> dict:
    """Manual brain mode: attach the user's AI picks and re-run the job
    (run_job resumes from the persisted words + scan). Returns the job."""
    if job.get("status") != "waiting_ai":
        raise ValueError("this job is not waiting for AI picks")
    clean = []
    for p in picks or []:
        if not isinstance(p, dict):
            continue
        try:
            s, e = float(p["start"]), float(p["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if e - s < 1.0:
            continue
        clean.append({"start": s, "end": e,
                      "title": str(p.get("title") or "")[:70],
                      "hook": str(p.get("hook") or "")[:80],
                      "caption": str(p.get("caption") or "")[:160],
                      "why": str(p.get("why") or "")[:220],
                      "score": p.get("score"),
                      "kind": p.get("kind") if p.get("kind") in brain.KINDS else "other"})
    if not clean:
        raise ValueError("no valid picks (each needs start and end in seconds)")
    job["manual_picks"] = clean
    job["status"] = "queued"
    job["stage"] = "queued"
    store.save(job)
    submit(job, store)
    return job


def rerender_clip(job: dict, store: JobStore, idx: int, new_options: dict | None) -> dict:
    """Re-render one clip with merged options, from the stored source file.
    The merged options become the job's options (so later re-renders inherit
    region/layout changes). Returns the updated clip dict."""
    clips = job.get("clips") or []
    if idx < 0 or idx >= len(clips):
        raise IndexError(f"clip {idx} does not exist")
    merged = dict(job.get("options") or {})
    if isinstance(new_options, dict):
        merged.update(new_options)
    options = clean_options(merged)
    job["options"] = options

    clip = clips[idx]
    src_str = clip.get("src") or job.get("source_path")
    if not src_str or not Path(src_str).exists():
        raise ValueError("the source file for this clip is gone")
    src = Path(src_str)
    offset = float(clip.get("src_offset") or 0.0)
    pr = media.probe(src)
    src_w, src_h = int(pr.get("width") or 0), int(pr.get("height") or 0)
    sec_dur = float(pr.get("duration") or 0)

    jdir = job_dir(store, job)
    words_path = jdir / "words.json"
    words = json.loads(words_path.read_text(encoding="utf-8")) if words_path.exists() else []
    words_for_render = words if offset == 0.0 else _shift_words(words, offset, sec_dur or None)

    name = clip.get("name") or f"clip{idx + 1:02d}"
    clip["name"] = name
    clip_for_render = dict(clip, start=float(clip["start"]) - offset, end=float(clip["end"]) - offset)
    try:
        result = render.render_clip(src, jdir / "clips", name, clip_for_render,
                                    words_for_render, src_w, src_h, options)
        clip.update(_basename_fields(result))
        clip["status"] = "done"
        clip["error"] = None
        clip["facecam"] = options.get("facecam")
        clip["game"] = options.get("game")
    except Exception as e:  # noqa: BLE001
        clip["status"] = "error"
        clip["error"] = str(e)
    store.save(job)
    return clip
