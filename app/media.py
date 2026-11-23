"""Getting video in: link download, probing, audio extraction, loudness.

Merged from PeakCut studio/media.py and NeonClipping (standalone.py/ytdl.py)
format strings and bot-check retry ladder (both MIT). No soundfile
dependency: WAV loading uses the stdlib wave module.
"""
import json
import os
import re
import subprocess
import wave
from pathlib import Path
from urllib.parse import urlparse

import numpy as np

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
    "twitch.tv", "www.twitch.tv", "clips.twitch.tv",
    "kick.com", "www.kick.com",
}

_BOT_CHECK = re.compile(r"not a bot|confirm you.?re not|sign in to confirm(?! your age)|http error 403|http error 429|too many requests", re.I)

# Errors where another route (player client / proxy / cookies) may succeed.
# Proxy/connection failures count too: a dead proxy must fall through to the
# direct attempts, never kill the whole ladder on the first try.
_RETRYABLE = re.compile(
    r"not a bot|confirm you.?re not|sign in|confirm your age|age-restricted|"
    r"no video formats|login required|http error 403|http error 429|too many requests|"
    r"unable to connect to proxy|proxyerror|connection refused|connection aborted|"
    r"connection reset|transporterror|remote end closed|broken pipe|eof occurred|"
    r"failed to establish a new connection|name or service not known|"
    r"temporary failure in name resolution|timed? ?out", re.I)


def run(cmd, **kw):
    """subprocess.run wrapper: capture, text, utf-8/replace, no console window.

    Returns the CompletedProcess (callers check .returncode themselves).
    Defaults can be overridden via kwargs (e.g. timeout=, stdin=).
    """
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("encoding", "utf-8")
    kw.setdefault("errors", "replace")
    kw.setdefault("creationflags", NO_WINDOW)
    return subprocess.run(cmd, **kw)


def site_allowed(url) -> bool:
    """Links are fetched by the server, so only known video sites are accepted
    (never arbitrary addresses). http/https, allowlisted host, no userinfo."""
    try:
        u = urlparse((url or "").strip())
        if u.username or u.password:
            return False
        _ = u.port  # raises ValueError on a malformed port
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme in ("http", "https") and host in ALLOWED_HOSTS


class _Quiet:
    def debug(self, m): pass
    def info(self, m): pass
    def warning(self, m): pass
    def error(self, m): pass


def _ydl(opts):
    import yt_dlp
    return yt_dlp.YoutubeDL({"noplaylist": True, "quiet": True, "no_warnings": True, "logger": _Quiet(),
                             "socket_timeout": 20, "concurrent_fragment_downloads": 4, **opts})


def _video_format(height):
    return f"bv*[height<={height}][vcodec^=avc1]+ba[ext=m4a]/bv*[height<={height}]+ba/b[height<={height}]/b"


def _is_blocked(err) -> bool:
    return bool(_RETRYABLE.search(str(err)))


def _proxy_url():
    """Optional HTTP proxy for yt-dlp (env CLIPFORGE_PROXY). On servers whose
    IP YouTube refuses (datacenter ranges), routing through a proxy on a
    normal connection is often the only way formats come back."""
    url = (os.environ.get("CLIPFORGE_PROXY") or "").strip()
    return url or None


def _cookie_browser():
    """yt-dlp cookiesfrombrowser tuple for a signed-in local browser, or None.

    Env CLIPFORGE_COOKIES_BROWSER overrides ("chromium", "chrome", "firefox",
    "none" to disable). Otherwise auto-detects Chromium/Chrome profiles
    (incl. the snap Chromium profile). Used as a last ladder rung — it
    unlocks age-restricted videos on a home connection. Never combined with
    the proxy (proven to break format listing)."""
    override = (os.environ.get("CLIPFORGE_COOKIES_BROWSER") or "").strip().lower()
    if override == "none":
        return None
    if override:
        return (override,)
    home = Path.home()
    candidates = [
        ("chromium", home / "snap/chromium/common/chromium/Default"),
        ("chromium", home / ".config/chromium/Default"),
        ("chrome", home / ".config/google-chrome/Default"),
        ("chromium", home / ".config/chromium"),
        ("chrome", home / ".config/google-chrome"),
    ]
    for name, profile in candidates:
        if (profile / "Cookies").is_file() or (profile / "Network/Cookies").is_file():
            return (name, str(profile))
    return None


def _ladder_attempts():
    """Extra yt-dlp opts per attempt, best route first. A configured proxy
    goes first (on a blocked-IP server it is the only route that works);
    browser cookies go last (age-restricted unlock on home connections)."""
    attempts = []
    proxy = _proxy_url()
    if proxy:
        attempts.append({"proxy": proxy, "nocheckcertificate": True})
    attempts.append({})
    attempts.append({"extractor_args": {"youtube": {"player_client": ["tv", "web_safari"]}}})
    attempts.append({"extractor_args": {"youtube": {"player_client": ["ios"]}}})
    cfb = _cookie_browser()
    if cfb:
        attempts.append({"cookiesfrombrowser": cfb})
    if proxy:
        # The tunnel/proxy can flap for a few seconds (restart, reconnect);
        # by the time the ladder gets here it has usually settled.
        attempts.append({"proxy": proxy, "nocheckcertificate": True})
    return attempts


def _with_ladder(fn):
    """Run fn(extra_opts) through the retry ladder: configured proxy (if
    any), default settings, YouTube player clients tv+web_safari, ios, then
    signed-in browser cookies (if a profile was found). Non-retryable
    errors on the first attempt propagate immediately."""
    attempts = _ladder_attempts()
    last = None
    for i, extra in enumerate(attempts):
        try:
            return fn(extra)
        except Exception as e:  # noqa: BLE001 - yt-dlp raises several error types
            last = e
            if _is_blocked(e) or i > 0:
                continue
            raise
    raise last


def _newest(dest_dir: Path, stem: str = None) -> Path:
    files = [p for p in Path(dest_dir).glob((stem + ".*") if stem else "*")
             if p.is_file() and p.suffix not in (".part", ".ytdl")]
    if not files:
        raise RuntimeError("Download finished but no file was produced.")
    return max(files, key=lambda p: p.stat().st_mtime)


def info(url) -> dict:
    """Metadata without downloading: {"title","uploader","duration","is_live","width","height","raw"}.
    Raises whatever yt-dlp raises on an unreadable link."""
    def fetch(extra):
        # Metadata must come back even when YouTube serves no formats to
        # this connection (live streams on blocked IPs) — preview uses it.
        opts = {"ignore_no_formats_error": True}
        opts.update(extra)
        with _ydl(opts) as ydl:
            return ydl.extract_info(url, download=False)

    i = _with_ladder(fetch)
    if i.get("_type") == "playlist" and i.get("entries"):
        entries = [e for e in i["entries"] if e]
        if entries:
            i = entries[0]
    return {"title": i.get("title") or "", "uploader": i.get("uploader") or i.get("channel") or "",
            "duration": float(i.get("duration") or 0), "is_live": bool(i.get("is_live")),
            "width": int(i.get("width") or 0), "height": int(i.get("height") or 0), "raw": i}


def probe(path) -> dict:
    """{"duration","width","height","has_audio","fps"} via ffprobe JSON.
    Raises RuntimeError if ffprobe fails or the file has no video stream."""
    p = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    if p.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {(p.stderr or '')[-500:]}")
    data = json.loads(p.stdout)
    streams = data.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not v:
        raise RuntimeError("This file has no video track.")
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    rot = str((v.get("side_data_list") or [{}])[0].get("rotation", "0"))
    if rot.lstrip("-") in ("90", "270"):
        w, h = h, w
    fps = 0.0
    rate = str(v.get("avg_frame_rate") or v.get("r_frame_rate") or "")
    if "/" in rate:
        try:
            num, den = rate.split("/", 1)
            fps = float(num) / float(den) if float(den) else 0.0
        except ValueError:
            fps = 0.0
    try:
        duration = float((data.get("format") or {}).get("duration") or v.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return {"duration": duration, "width": w, "height": h,
            "has_audio": any(s.get("codec_type") == "audio" for s in streams), "fps": fps}


def _progress_hook(progress_cb):
    def hook(d):
        if not progress_cb:
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if d.get("status") == "downloading" and total:
            try:
                progress_cb(min(1.0, max(0.0, d.get("downloaded_bytes", 0) / total)))
            except Exception:
                pass
    return hook


def download(url, dest_dir, height=1080, progress_cb=None) -> Path:
    """Download a video (H.264 ≤height + m4a audio preferred), merged to mp4.
    Retries through the player-client ladder on bot-check errors.
    progress_cb(fraction 0..1) is called as bytes arrive. Returns the file Path."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    def fetch(extra):
        opts = {"format": _video_format(height), "merge_output_format": "mp4",
                "outtmpl": str(dest_dir / "source.%(ext)s"), "progress_hooks": [_progress_hook(progress_cb)]}
        opts.update(extra)
        with _ydl(opts) as ydl:
            ydl.download([url])

    _with_ladder(fetch)
    return _newest(dest_dir, "source")


def download_audio(url, dest_dir, progress_cb=None) -> Path:
    """Audio only: a few seconds even for long videos. Returns the file Path."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    def fetch(extra):
        opts = {"format": "ba[ext=m4a]/ba/worst", "outtmpl": str(dest_dir / "audio_src.%(ext)s"),
                "progress_hooks": [_progress_hook(progress_cb)]}
        opts.update(extra)
        with _ydl(opts) as ydl:
            ydl.download([url])

    _with_ladder(fetch)
    return _newest(dest_dir, "audio_src")


def download_section(url, dest_dir, start, end, height=1080) -> Path:
    """Just one stretch of the video (keyframe-aligned cut; use find_offset to
    pin the true start). Returns the file Path."""
    from yt_dlp.utils import download_range_func
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    def fetch(extra):
        opts = {"format": _video_format(height), "merge_output_format": "mp4",
                "outtmpl": str(dest_dir / "section.%(ext)s"),
                "download_ranges": download_range_func(None, [(max(0.0, float(start)), float(end))]),
                "force_keyframes_at_cuts": False}
        opts.update(extra)
        with _ydl(opts) as ydl:
            ydl.download([url])

    _with_ladder(fetch)
    return _newest(dest_dir, "section")


def extract_audio(src, out_wav, start=0.0, end=None, sr=16000) -> Path:
    """Extract [start, end) as mono pcm_s16le WAV at sr Hz. first_pts=0 pads
    any gap before the audio starts, so second 0 of the wav is `start`.
    Raises RuntimeError on ffmpeg failure."""
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{float(start):.3f}"]
    if end is not None:
        cmd += ["-t", f"{max(0.0, float(end) - float(start)):.3f}"]
    cmd += ["-i", str(src), "-vn", "-ac", "1", "-ar", str(int(sr)),
            "-af", "aresample=async=1:first_pts=0", "-c:a", "pcm_s16le", str(out_wav)]
    p = run(cmd, timeout=3600)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg audio extraction failed: {(p.stderr or '')[-800:]}")
    return out_wav


def load_wav_mono(path):
    """Load a WAV file as (np.float32 mono array in -1..1, sample_rate).
    Stdlib wave only; multi-channel input is downmixed by averaging."""
    with wave.open(str(path), "rb") as w:
        n_channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        sr = w.getframerate()
        frames = w.readframes(w.getnframes())
    if sampwidth == 2:
        data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    elif sampwidth == 1:
        data = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sampwidth == 4:
        data = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise RuntimeError(f"Unsupported WAV sample width: {sampwidth}")
    if n_channels > 1 and len(data):
        data = data[: len(data) // n_channels * n_channels].reshape(-1, n_channels).mean(axis=1)
    return data.astype(np.float32), int(sr)


def loudness_per_second(wav_path) -> list:
    """RMS level in dB for each second of audio (20*log10(rms+1e-9)).

    Worked out ten minutes at a time to bound memory, whatever the length."""
    audio, sr = load_wav_mono(wav_path)
    n = len(audio) // sr if sr else 0
    if n == 0:
        return []
    out = np.empty(n, dtype=np.float32)
    piece = 600  # seconds per chunk
    for a in range(0, n, piece):
        b = min(n, a + piece)
        x = audio[a * sr: b * sr].reshape(b - a, sr).astype(np.float32)
        out[a:b] = 20 * np.log10(np.sqrt((x ** 2).mean(axis=1)) + 1e-9)
    return [float(v) for v in out]


def _to_8k(x, sr):
    """Crude decimation to 8 kHz (enough for correlation matching)."""
    if sr == 8000 or not len(x):
        return x
    if sr > 8000 and sr % 8000 == 0:
        return x[:: sr // 8000]
    idx = np.linspace(0, len(x) - 1, int(len(x) * 8000 / sr)).astype(int)
    return x[idx]


def find_offset(full_wav, section_wav, approx_start, search=20.0) -> float:
    """Where in the full audio (seconds) a downloaded section really begins,
    by FFT cross-correlation of the section against the full audio around
    approx_start ± search (8 kHz mono). Falls back to approx_start when the
    section is too short or too quiet to match."""
    full, fsr = load_wav_mono(full_wav)
    part, psr = load_wav_mono(section_wav)
    full = _to_8k(full, fsr)
    part = _to_8k(part, psr)
    sr = 8000
    n = min(len(part), 30 * sr)
    expected = float(approx_start)
    if n < sr or float(np.abs(part[:n]).mean()) < 0.001:
        return expected
    a0 = max(0, int((expected - search) * sr))
    hay = full[a0: int((expected + search) * sr) + n].astype(np.float32)
    needle = part[:n].astype(np.float32)
    if len(hay) < n:
        return expected
    size = 1 << (len(hay) + n).bit_length()
    corr = np.fft.irfft(np.fft.rfft(hay, size) * np.conj(np.fft.rfft(needle, size)), size)[: len(hay) - n + 1]
    return (a0 + int(np.argmax(corr))) / sr


def frame_at(src, t, out_jpg, width=960) -> Path:
    """One frame at time t (seconds), scaled to `width` px wide. Returns out_jpg."""
    out_jpg = Path(out_jpg)
    out_jpg.parent.mkdir(parents=True, exist_ok=True)
    p = run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, float(t)):.3f}", "-i", str(src),
             "-frames:v", "1", "-vf", f"scale={int(width)}:-2", "-q:v", "3", str(out_jpg)], timeout=120)
    if p.returncode != 0 or not out_jpg.is_file():
        raise RuntimeError(f"ffmpeg frame extraction failed: {(p.stderr or '')[-500:]}")
    return out_jpg


def _fetch_bytes(url, proxy, timeout=30, tries=3) -> bytes:
    """GET a URL (through the configured proxy when set), resuming with
    Range requests when the connection drops mid-body — the tunnel path
    used for YouTube on servers is slow and drops often."""
    import urllib.request
    import urllib.error
    # Explicit proxy wins; otherwise honour the environment's proxy
    # (http_proxy/https_proxy) — ProxyHandler({}) would DISABLE proxies
    # entirely and break every fetch on networks that require one
    # (proven 2026-11-23: SSL WRONG_VERSION_NUMBER on direct egress).
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}) if proxy
        else urllib.request.ProxyHandler())
    data = b""
    for _ in range(tries):
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                 "AppleWebKit/537.36 (KHTML, like Gecko) "
                                 "Chrome/124.0 Safari/537.36",
                   "Referer": "https://www.youtube.com/"}
        if data:
            headers["Range"] = f"bytes={len(data)}-"
        req = urllib.request.Request(url, headers=headers)
        try:
            with opener.open(req, timeout=timeout) as r:
                if data and r.status != 206:  # server ignored Range: restart
                    data = b""
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    data += chunk
            return data
        except Exception:
            if not data:
                continue
            # partial body kept; next try resumes from len(data)
            continue
    if data:
        return data
    raise RuntimeError(f"Could not fetch {url[:80]}")


def _hls_frame(playlist_url, out_jpg, width=960) -> Path:
    """One frame from an HLS playlist by fetching a single segment
    directly (init segment + last media segment) and decoding it
    locally. Far fewer round-trips than letting ffmpeg walk the
    playlist through a slow proxy."""
    import urllib.parse
    proxy = _proxy_url()
    pl = _fetch_bytes(playlist_url, proxy).decode("utf-8", "replace")
    segs, init = [], None
    for line in pl.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MAP:"):
            for part in line.split(","):
                if part.startswith('URI="'):
                    init = urllib.parse.urljoin(playlist_url, part[5:].strip('"'))
        elif line and not line.startswith("#"):
            segs.append(urllib.parse.urljoin(playlist_url, line))
    if not segs:
        raise RuntimeError("No segments in HLS playlist.")
    data = (_fetch_bytes(init, proxy) if init else b"") + _fetch_bytes(segs[-1], proxy)
    seg = Path(out_jpg).with_suffix(".seg.ts")
    seg.parent.mkdir(parents=True, exist_ok=True)
    seg.write_bytes(data)
    try:
        p = run(["ffmpeg", "-y", "-v", "error", "-i", str(seg), "-frames:v", "1",
                 "-vf", f"scale={int(width)}:-2", "-q:v", "3", str(out_jpg)], timeout=60)
        if p.returncode != 0 or not Path(out_jpg).is_file():
            raise RuntimeError(f"Segment frame decode failed: {(p.stderr or '')[-300:]}")
    finally:
        seg.unlink(missing_ok=True)
    return Path(out_jpg)


def _dash_segments_frame(fmt, out_jpg, width=960, live=False) -> Path:
    """One frame from a YouTube http_dash_segments format (live/post-live
    broadcasts): fetch the init segment (sq=0) + one media segment and
    decode locally, like _hls_frame. Plain ffmpeg on the base URL fails
    with Input/output error — these URLs serve a single segment per sq=
    request and do not support byte-range seeking."""
    import re
    base, count, sq_values = None, 0, []
    for fr in fmt.get("fragments") or []:
        u = fr.get("url") or ""
        if not u and fr.get("path"):
            u = (fmt.get("fragment_base_url") or "") + fr["path"]
        m = re.search(r"sq[=/](\d+)", u)
        if m:
            sq_values.append(int(m.group(1)))
            if base is None:
                base = u
                count = int(fr.get("fragment_count") or 0)
    if not base:
        raise RuntimeError("No sq= fragment URL in DASH format.")
    sep = "=" if "sq=" in base else "/"
    if live:
        # Live broadcasts often carry no fragment_count; the fragments list
        # itself names the segments that exist — take the newest one.
        # (Fetching sq=1 blindly grabs the stream's first segment, which
        # is long gone on a multi-hour live: the PC 400s of 2026-11-23.)
        if sq_values:
            seg_no = max(1, max(sq_values) - 1)
        elif count:
            seg_no = max(1, count - 2)
        else:
            seg_no = 1
    else:
        seg_no = max(1, int(count * 0.1)) if count else 10
    proxy = _proxy_url()
    init_url = re.sub(r"sq[=/]\d+", f"sq{sep}0", base)
    seg_url = re.sub(r"sq[=/]\d+", f"sq{sep}{seg_no}", base)
    data = _fetch_bytes(init_url, proxy) + _fetch_bytes(seg_url, proxy)
    seg = Path(out_jpg).with_suffix(".seg.mp4")
    seg.parent.mkdir(parents=True, exist_ok=True)
    seg.write_bytes(data)
    try:
        p = run(["ffmpeg", "-y", "-v", "error", "-i", str(seg), "-frames:v", "1",
                 "-vf", f"scale={int(width)}:-2", "-q:v", "3", str(out_jpg)], timeout=60)
        if p.returncode != 0 or not Path(out_jpg).is_file():
            raise RuntimeError(f"DASH segment frame decode failed: {(p.stderr or '')[-300:]}")
    finally:
        seg.unlink(missing_ok=True)
    return Path(out_jpg)


def preview_frame(url, out_jpg, meta=None, width=960) -> Path:
    """A REAL frame from the video at `url` (not the thumbnail), so the user
    can drag-select the facecam/gameplay sections on the actual layout.

    Resolves a direct stream URL through the retry ladder, then grabs one
    frame with ffmpeg. Live streams: the current frame. VODs: ~10% in
    (past intros), clamped to 3–30 s. When a proxy is configured the frame
    fetch goes through it too — signed stream URLs are bound to the egress
    IP that resolved them. Raises on failure (callers fall back to the
    thumbnail)."""
    def fetch(extra):
        opts = {"format": "b[height<=720]/bv*[height<=720]+ba/b"}
        opts.update(extra)
        with _ydl(opts) as ydl:
            return ydl.extract_info(url, download=False)

    def pick(i):
        """Best format dict for a preview frame, or None."""
        if i.get("url"):
            return i
        for f in i.get("requested_formats") or []:
            if f.get("url") and f.get("vcodec") not in (None, "none"):
                return f
        cands = [f for f in (i.get("formats") or [])
                 if f.get("url") and f.get("vcodec") not in (None, "none")]
        if cands:
            # A preview frame only needs to be clear enough to place boxes:
            # prefer 360–480p so the grab is fast even on slow connections.
            # HLS (m3u8) formats first: their frame grab is far more reliable
            # than http_dash_segments, whose live segment numbering is
            # often unknown (the PC 400s of 2026-11-23).
            cands.sort(key=lambda f: ("dash" in str(f.get("protocol") or ""),
                                      f.get("height") or 0))
            mid = [f for f in cands if 360 <= (f.get("height") or 0) <= 480]
            if mid:
                return mid[0]
            low = [f for f in cands if (f.get("height") or 9999) <= 720]
            return low[-1] if low else cands[0]
        return None

    # Reuse the caller's already-resolved info when it carries formats —
    # resolving twice roughly doubles preview time on slow connections.
    # But cached formats can be unusable (e.g. DASH without fragments, or
    # URLs resolved through a different ladder rung), so if every stage
    # fails on them, re-resolve fresh once and try again.
    out_jpg = Path(out_jpg)
    out_jpg.parent.mkdir(parents=True, exist_ok=True)
    errors = []

    def try_stages(fmt, i):
        stream_url = fmt["url"]
        is_live = bool(i.get("is_live") or (meta or {}).get("is_live"))
        duration = float(i.get("duration") or (meta or {}).get("duration") or 0)
        proto = str(fmt.get("protocol") or "")
        # HLS (all live streams): fetch one segment directly — much faster
        # than ffmpeg walking the playlist through a slow proxy. For VODs
        # the last segment is the end of the video, which is fine for
        # placing facecam/gameplay boxes.
        if "m3u8" in proto or ".m3u8" in stream_url or "/manifest/" in stream_url:
            try:
                return _hls_frame(stream_url, out_jpg, width=width)
            except Exception as e:  # noqa: BLE001
                errors.append(f"hls: {e}")
                print(f"[clipforge] preview-frame hls stage failed: {e}", flush=True)
        # YouTube live/post-live broadcasts serve http_dash_segments formats:
        # one segment per sq= request. Fetch init + one segment directly —
        # ffmpeg on the base URL dies with Input/output error.
        if "dash_segments" in proto or fmt.get("fragments"):
            try:
                return _dash_segments_frame(fmt, out_jpg, width=width, live=is_live)
            except Exception as e:  # noqa: BLE001
                errors.append(f"dash: {e}")
                print(f"[clipforge] preview-frame dash stage failed: {e}", flush=True)
        # ffmpeg fallback: on a DASH base URL ffmpeg dies (single-segment
        # endpoint) — if any HLS format exists, point ffmpeg at its playlist
        # instead, which it handles natively.
        if "dash" in proto:
            for f in (i.get("formats") or []):
                if f.get("url") and "m3u8" in str(f.get("protocol") or ""):
                    stream_url = f["url"]
                    break
        cmd = ["ffmpeg", "-y", "-v", "error", "-rw_timeout", "25000000"]
        proxy = _proxy_url()
        if proxy:
            cmd += ["-http_proxy", proxy]
        if not is_live:
            t = max(3.0, min(30.0, duration * 0.1)) if duration else 10.0
            cmd += ["-ss", f"{t:.3f}"]
        cmd += ["-i", stream_url, "-frames:v", "1", "-vf", f"scale={int(width)}:-2",
                "-q:v", "3", str(out_jpg)]
        p = run(cmd, timeout=90)
        if p.returncode != 0 or not out_jpg.is_file():
            tail = (p.stderr or "")[-300:]
            print(f"[clipforge] preview-frame ffmpeg stage failed: {tail}", flush=True)
            raise RuntimeError(f"ffmpeg: {tail}")
        return out_jpg

    cached_raw = (meta or {}).get("raw") or {}
    fmt = pick(cached_raw)
    if fmt and fmt.get("url"):
        try:
            return try_stages(fmt, cached_raw)
        except Exception as e:  # noqa: BLE001
            errors.append(f"cached formats: {e}")
            print(f"[clipforge] preview-frame cached formats failed, re-resolving: {e}",
                  flush=True)
    else:
        print("[clipforge] preview-frame: no usable format in cached info, resolving fresh",
              flush=True)
    try:
        i = _with_ladder(fetch)
    except Exception as e:  # noqa: BLE001
        print(f"[clipforge] preview-frame resolve failed: {e}", flush=True)
        raise RuntimeError("Preview frame failed (" + " | ".join(errors) +
                           f" | resolve: {e})") from e
    fmt = pick(i)
    if not fmt or not fmt.get("url"):
        print("[clipforge] preview-frame: no playable stream URL after fresh resolve",
              flush=True)
        raise RuntimeError("Preview frame failed (" + " | ".join(errors) +
                           " | no playable stream URL)")
    try:
        return try_stages(fmt, i)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("Preview frame failed (" + " | ".join(errors) +
                           f" | {e})") from e


def friendly_error(exc, url="") -> str:
    """Turn common yt-dlp failures into the fix the user needs.
    Ported from stream-clips media.friendly()."""
    text = str(exc)
    low = text.lower()
    if "not a bot" in low or ("sign in to confirm" in low and "your age" not in low):
        return ("YouTube is asking to confirm you're not a bot. Wait a while, or download the "
                "video in your browser and upload the file here.")
    if "confirm your age" in low or "age-restricted" in low or "age restricted" in low:
        return ("YouTube needs a login for this video (age-restricted). "
                "Download it in your browser while signed in, then upload the file here.")
    if "no video formats" in low:
        return ("YouTube isn't serving this video to this connection (common on servers and "
                "VPNs, especially for live streams). Download it in your browser, then upload "
                "the file here.")
    if "proxy" in low or "connection refused" in low:
        return ("Couldn't reach YouTube from this connection right now. Wait a minute and try "
                "again, or download the video in your browser and upload the file here.")
    if "kick" in str(url) and ("403" in text or "forbidden" in low):
        return "Kick blocked the download. Download it in your browser, then upload the file here."
    if "subscriber" in low or "sub-only" in low or "only available to" in low:
        return ("This VOD is subscriber-only. Download it from an account that can watch it, "
                "then upload the file here.")
    return text


def chat_speed(url, meta=None, work_dir=None):
    """Chat messages per 10 s from a YouTube live replay, or None.

    Uses yt-dlp's live_chat subtitle track (no API key). Only attempted for
    YouTube videos that were live. Fail-soft: any problem returns None.
    Ported from stream-clips listen.chat_speed()."""
    import tempfile
    try:
        raw = (meta or {}).get("raw") or meta or {}
        if "youtube" not in str(raw.get("extractor_key", "youtube")).lower():
            return None
        if raw.get("live_status") not in ("was_live", "post_live") and not raw.get("was_live"):
            return None
        work = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="cfchat"))
        work.mkdir(parents=True, exist_ok=True)
        cached = work / "chat.json"
        if cached.exists():
            try:
                return json.loads(cached.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass

        def fetch(extra):
            opts = {"skip_download": True, "writesubtitles": True, "subtitleslangs": ["live_chat"],
                    "outtmpl": str(work / "chat.%(ext)s")}
            opts.update(extra)
            with _ydl(opts) as ydl:
                ydl.download([url])

        try:
            _with_ladder(fetch)
        except Exception:  # noqa: BLE001 - chat is a bonus signal, never a failed job
            pass
        files = sorted(work.glob("chat*.live_chat.json*"))
        if not files:
            return None
        counts = {}
        with open(files[0], encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                try:
                    item = json.loads(line)["replayChatItemAction"]
                    bucket = int(item.get("videoOffsetTimeMsec", 0)) // 10000
                except (ValueError, KeyError, TypeError):
                    continue
                counts[bucket] = counts.get(bucket, 0) + len(item.get("actions") or [1])
        for f in files:
            f.unlink(missing_ok=True)
        if not counts:
            return None
        rate = [0] * (max(counts) + 1)
        for k, n in counts.items():
            rate[k] = n
        try:
            cached.write_text(json.dumps(rate), encoding="utf-8")
        except OSError:
            pass
        return rate
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ discovery
_WHEN = {"hour": 1, "today": 2, "week": 3, "month": 4}


def _search_filter(when):
    """YouTube's `sp` search parameter: sort by views + date window + long (>20 min)."""
    import base64
    inner = bytes([0x08, _WHEN.get(when, 2), 0x18, 2])
    raw = bytes([0x08, 3, 0x12, len(inner)]) + inner
    return base64.b64encode(raw).decode()


def _flat_listing(url, limit=20):
    """yt-dlp flat-playlist entries for a search/channel page (fail-soft [])."""
    try:
        def fetch(extra):
            opts = {"extract_flat": True, "playlistend": int(limit)}
            opts.update(extra)
            with _ydl(opts) as ydl:
                return ydl.extract_info(url, download=False)
        data = _with_ladder(fetch)
        return [e for e in (data.get("entries") or []) if e]
    except Exception:  # noqa: BLE001
        return []


def search_streams(topic, when="today", limit=20, suffix="stream"):
    """Biggest recent long streams for a topic via YouTube search (no API key).
    Returns [{url, title, channel, views, duration, live, from}]."""
    from urllib.parse import quote_plus
    query = " ".join(x for x in (str(topic or "").strip(), suffix) if x)
    url = ("https://www.youtube.com/results?search_query=" + quote_plus(query)
           + "&sp=" + quote_plus(_search_filter(when)))
    out = []
    for e in _flat_listing(url, limit):
        if not e.get("id"):
            continue
        out.append({"url": f"https://www.youtube.com/watch?v={e['id']}",
                    "title": e.get("title") or "",
                    "channel": e.get("channel") or e.get("uploader") or "?",
                    "views": int(e.get("view_count") or 0),
                    "duration": float(e.get("duration") or 0),
                    "live": e.get("live_status") == "is_live",
                    "from": "youtube search"})
    out.sort(key=lambda s: -s["views"])
    return out


def watchlist_newest(platform, handle, words=None, max_age_h=48):
    """A creator's newest video whose title matches `words` (any), or None.
    platform: twitch | youtube | kick. Ported from stream-clips discover."""
    import time as _time
    handle = str(handle or "").strip()
    if not handle:
        return None
    if platform == "twitch":
        page = f"https://www.twitch.tv/{handle}/videos?filter=archives&sort=time"
    elif platform == "youtube":
        page = f"https://www.youtube.com/{handle if handle.startswith('@') else '@' + handle}/streams"
    elif platform == "kick":
        page = f"https://kick.com/{handle}/videos"
    else:
        return None
    for e in _flat_listing(page, 4):
        title = e.get("title") or ""
        if words and not any(w in title.lower() for w in words):
            continue
        url = e.get("url") or e.get("webpage_url") or ""
        if not str(url).startswith("http"):
            if not e.get("id"):
                continue
            url = f"https://www.youtube.com/watch?v={e['id']}"
        try:
            full = info(url)
        except Exception:  # noqa: BLE001
            continue
        raw = full.get("raw") or {}
        when = raw.get("timestamp") or raw.get("release_timestamp") or 0
        if when and _time.time() - when > max_age_h * 3600:
            return None
        return {"url": url, "title": title,
                "channel": full.get("uploader") or handle,
                "views": int(raw.get("view_count") or raw.get("concurrent_view_count") or 0),
                "duration": float(full.get("duration") or 0),
                "live": bool(full.get("is_live")), "from": "watchlist"}
    return None


def resolve_live(url, height=1080) -> dict:
    """Direct stream URLs for a live link: {"urls": [...], "title": str}.
    Raises ValueError with a user-facing message if the link is not
    currently live.
    `height` caps the recorded quality (the streamer's own top quality
    up to that cap) — facecam crops upscale badly from low-res sources,
    so live recording defaults to 1080p."""
    h = int(height or 1080)
    def fetch(extra):
        opts = {"format": f"b[height<={h}]/bv*[height<={h}]+ba/b"}
        opts.update(extra)
        with _ydl(opts) as ydl:
            return ydl.extract_info(url, download=False)

    i = _with_ladder(fetch)
    if not i.get("is_live"):
        raise ValueError("This stream is not live right now — it has ended or "
                         "not started yet. To clip the recording, use the New Job tab.")
    urls = []
    height = int(i.get("height") or 0)
    if i.get("url"):
        urls.append(i["url"])
    for f in i.get("requested_formats") or []:
        if f.get("url"):
            urls.append(f["url"])
            if not height and f.get("vcodec") not in (None, "none"):
                height = int(f.get("height") or 0)
    if not urls:
        for f in reversed(i.get("formats") or []):
            if f.get("url") and f.get("protocol", "").startswith(("http", "https")):
                urls.append(f["url"])
                height = int(f.get("height") or 0)
                break
    return {"urls": urls, "title": i.get("title") or "", "height": height}
