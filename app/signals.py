"""Whole-video signal scan: optical-flow motion + audio spikes, in parallel.

Signals (all local, no model download):
  motion  - OpenCV Farneback optical flow at 160x90, 90th-percentile magnitude per second (fast objects,
            explosions, car flips); hard scene cuts are detected and excluded from motion, counted as cut density.
  audio   - loudness JUMP vs the local 2-minute baseline (sudden boom/shout beats constantly loud music).
  shout   - share of 2-4 kHz energy (voices raised, screams, impacts) weighted by loudness jump.
  cuts    - editing/camera-cut density.
  excite  - excitement from what is said (excite.seconds_signal), when a transcript exists.
The source is split into whole-minute chunks decoded by parallel FFmpeg processes (decode is the bottleneck).
Candidates only; semantic re-ranking happens in brain.py.

Reference: NeonClipping highlights.py (MIT).
"""
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

from . import excite as _excite
from . import media

FPS = 3
W, H = 160, 90
CUT = 38.0   # mean abs diff above this = hard cut

try:
    import cv2
except Exception:  # pragma: no cover - cv2 optional at runtime
    cv2 = None


def _chunk(source, start, length, report):
    cmd = ['ffmpeg', '-v', 'error', '-ss', f'{start}', '-t', f'{length}', '-i', str(source), '-an',
           '-vf', f'fps={FPS},scale={W}:{H}', '-pix_fmt', 'gray', '-f', 'rawvideo', 'pipe:1']
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=W * H * 64,
                         creationflags=media.NO_WINDOW)
    flow, cuts, prev, n = [], [], None, 0
    try:
        while True:
            raw = p.stdout.read(W * H)
            while raw and len(raw) < W * H:
                more = p.stdout.read(W * H - len(raw))
                if not more:
                    break
                raw += more
            if len(raw) != W * H:
                break
            cur = np.frombuffer(raw, np.uint8).reshape(H, W)
            if prev is None:
                flow.append(0.); cuts.append(0.)
            else:
                if float(np.mean(cv2.absdiff(cur, prev))) > CUT:
                    flow.append(0.); cuts.append(1.)
                else:
                    f = cv2.calcOpticalFlowFarneback(prev, cur, None, .5, 2, 9, 2, 5, 1.1, 0)
                    flow.append(float(np.percentile(np.hypot(f[..., 0], f[..., 1]), 90))); cuts.append(0.)
            prev = cur; n += 1
        p.wait()
    finally:
        if p.poll() is None:
            p.kill(); p.wait()
        p.stdout.close()
    report(length)
    k = len(flow) // FPS * FPS
    f = np.array(flow[:k]).reshape(-1, FPS).mean(1) if k else np.zeros(0)
    c = np.array(cuts[:k]).reshape(-1, FPS).sum(1) if k else np.zeros(0)
    return f, c


def _motion(source, duration, report):
    workers = max(2, min(12, (os.cpu_count() or 4) - 2))
    size = max(60, int(np.ceil(duration / workers / 60) * 60))          # whole-minute chunks
    starts = list(range(0, int(np.ceil(duration)), size))
    done = [0.]

    def rep(sec):
        done[0] += sec
        report(min(done[0], duration))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {pool.submit(_chunk, source, s, min(size, duration - s), rep): s for s in starts}
        parts = {jobs[j]: j.result() for j in as_completed(jobs)}
    f = np.concatenate([parts[s][0] for s in starts]) if starts else np.zeros(0)
    c = np.concatenate([parts[s][1] for s in starts]) if starts else np.zeros(0)
    return f, c


def _audio(source):
    cmd = ['ffmpeg', '-nostdin', '-v', 'error', '-i', str(source), '-vn', '-ac', '1', '-ar', '8000', '-f', 's16le', 'pipe:1']
    raw = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, timeout=4 * 3600,
                         creationflags=media.NO_WINDOW).stdout
    x = np.frombuffer(raw[:len(raw) // 2 * 2], np.int16).astype(np.float32) / 32768
    n = len(x) // 8000
    if n == 0:
        return np.zeros(0), np.zeros(0)
    x = x[:n * 8000].reshape(n, 8000)
    rms = np.sqrt((x ** 2).mean(1))
    spec = np.abs(np.fft.rfft(x * np.hanning(8000), axis=1)) ** 2          # 1 Hz bins
    hf = spec[:, 2000:4000].sum(1) / (spec.sum(1) + 1e-9)
    return rms, hf


def _z(x, n, win=120):
    """Local z-score vs a rolling baseline, clipped to 0..1 (z of 3.5 = 1)."""
    out = np.zeros(n)
    if not len(x):
        return out
    y = np.zeros(n); y[:min(n, len(x))] = x[:n]
    win = max(1, min(win, n)); k = np.ones(win) / win
    mu = np.convolve(y, k, 'same'); sd = np.sqrt(np.maximum(np.convolve(y * y, k, 'same') - mu * mu, 0)) + 1e-6
    return np.clip((y - mu) / sd / 3.5, 0, 1)


def _spike(rms, n, window=180):
    """Volume spike: dB above the typical level of the minutes around each
    second (60th percentile of a ±90 s window), normalised so +7 dB = 1.
    Ported from stream-clips listen.spikes(). Returns (norm 0..1, raw dB)."""
    if not len(rms):
        return np.zeros(n), np.zeros(n)
    db = 20 * np.log10(np.asarray(rms, dtype=np.float32) + 1e-5)
    y = np.full(n, float(np.median(db)) if len(db) else -40.0, dtype=np.float32)
    y[:min(n, len(db))] = db[:n]
    step = 5
    coarse = y[::step]
    half = max(1, window // step // 2)
    pad = np.pad(coarse, half, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(pad, 2 * half + 1)
    base = np.repeat(np.percentile(windows, 60, axis=1), step)[:n]
    raw = np.clip(y - base, 0, None)
    return np.clip(raw / 7.0, 0, 1), raw


def _chat_norm(chat_rate, n):
    """Chat messages per 10 s -> per-second 0..1 (3x the median rate = 1)."""
    if not chat_rate:
        return np.zeros(n), False
    per_sec = np.zeros(n)
    for i, count in enumerate(chat_rate):
        lo, hi = i * 10, min(n, (i + 1) * 10)
        if lo < n:
            per_sec[lo:hi] = float(count) / 10.0
    med = float(np.median(per_sec[per_sec > 0])) if (per_sec > 0).any() else 0.0
    if med <= 0:
        return np.zeros(n), False
    return np.clip((per_sec / med - 1.0) / 2.0, 0, 1), True


def scan(source, duration, words=None, motion_on=True, report=lambda s: None,
         chat_rate=None, game=None) -> dict:
    """Per-second signal arrays over the whole source.

    Returns {"n", "motion", "cuts", "loud", "shout", "spike", "spike_db",
    "chat", "game", "game_types", "excite", "signal", "has_audio",
    "motion_on", "has_chat", "has_game"} with every array a list[float] of
    length n = ceil(duration). `report(seconds_done)` is called as motion
    chunks finish. If cv2 is unavailable, motion_on comes back False with
    zeroed motion/cuts. Never raises on a silent/short source (zeros
    instead); raises only if the source cannot be decoded at all by ffmpeg
    audio pipe timeout etc.

    chat_rate: chat messages per 10 s (media.chat_speed) or None.
    game: gameevents.scan() result or None.
    """
    duration = float(duration)
    n = max(1, int(np.ceil(duration)))
    if motion_on and cv2 is None:
        motion_on = False
    with ThreadPoolExecutor(max_workers=2) as pool:
        m = pool.submit(_motion, source, duration, report) if motion_on else pool.submit(lambda: (np.zeros(0), np.zeros(0)))
        a = pool.submit(_audio, source)
        (flow, cuts_raw), (rms, hf) = m.result(), a.result()
    has_audio = len(rms) > 0
    motion = _z(flow, n)
    cut = _z(cuts_raw, n)
    loud = _z(rms, n) if has_audio else np.zeros(n)
    shout = np.clip(_z(hf * rms, n) if has_audio else np.zeros(n), 0, 1)
    spike, spike_db = _spike(rms, n) if has_audio else (np.zeros(n), np.zeros(n))
    chat, has_chat = _chat_norm(chat_rate, n)
    game_arr = np.zeros(n)
    game_types = [""] * n
    has_game = bool(game and game.get("per_second"))
    if has_game:
        ps = game["per_second"]
        game_arr[:min(n, len(ps))] = ps[:n]
        gt = game.get("types") or []
        game_types[:min(n, len(gt))] = gt[:n]
    exc = np.zeros(n)
    if words:
        try:
            e = _excite.seconds_signal(words, duration)
            exc[:min(n, len(e))] = e[:n]
        except Exception:
            exc = np.zeros(n)
    loud_eff = .6 * loud + .4 * spike
    if motion_on and has_audio:
        sig = .35 * motion + .30 * loud_eff + .20 * shout + .15 * cut
    elif has_audio:
        sig = .55 * loud_eff + .45 * shout
    elif motion_on:
        sig = .7 * motion + .3 * cut
    else:
        sig = np.zeros(n)
    if words:
        sig = .75 * sig + .25 * exc
    if has_game:
        sig = .7 * sig + .3 * game_arr
    if has_chat:
        sig = .85 * sig + .15 * chat
    return {"n": n,
            "motion": [float(v) for v in motion], "cuts": [float(v) for v in cut],
            "loud": [float(v) for v in loud], "shout": [float(v) for v in shout],
            "spike": [float(v) for v in spike], "spike_db": [float(v) for v in spike_db],
            "chat": [float(v) for v in chat], "game": [float(v) for v in game_arr],
            "game_types": game_types,
            "excite": [float(v) for v in exc], "signal": [float(v) for v in sig],
            "has_audio": bool(has_audio), "motion_on": bool(motion_on),
            "has_chat": bool(has_chat), "has_game": bool(has_game)}


def window_features(scan, start, end) -> dict:
    """Mean of each signal over [start, end): {"loud","shout","motion","cuts","words"}.

    None when a signal is absent: motion/cuts None if motion was off,
    loud/shout None if the source has no audio. "words" is the excite mean
    (0.0 when no transcript was scanned)."""
    n = int(scan.get("n") or 0)
    lo = max(0, min(n, int(start)))
    hi = max(lo + 1, min(n, int(np.ceil(end)))) if n else lo + 1

    def mean(key):
        arr = scan.get(key) or []
        seg = arr[lo:hi]
        return float(sum(seg) / len(seg)) if seg else 0.0

    return {"loud": mean("loud") if scan.get("has_audio") else None,
            "shout": mean("shout") if scan.get("has_audio") else None,
            "spike": mean("spike") if scan.get("has_audio") else None,
            "motion": mean("motion") if scan.get("motion_on") else None,
            "cuts": mean("cuts") if scan.get("motion_on") else None,
            "chat": mean("chat") if scan.get("has_chat") else None,
            "game": mean("game") if scan.get("has_game") else None,
            "words": mean("excite")}


def candidates(scan, duration, count, min_len, max_len) -> list:
    """Greedy non-overlapping peak windows from the combined signal.

    Smooths the signal over 7 s, takes the strongest centers first, window
    length = clamp((min_len+max_len)/2, min_len, max_len). Each candidate
    follows the shared Candidate shape; brain.py retitles and re-scores.
    """
    duration = float(duration)
    n = int(scan.get("n") or max(1, int(np.ceil(duration))))
    sig = np.asarray(scan.get("signal") or [], dtype=float)
    if len(sig) < n:
        sig = np.pad(sig, (0, n - len(sig)))
    sig = sig[:n]
    width = min(n, 7)
    smooth = np.convolve(sig, np.ones(width) / width, 'same') if n else sig
    peak = float(smooth.max()) if len(smooth) else 0.0
    length = min(max((float(min_len) + float(max_len)) / 2.0, float(min_len)), float(max_len))
    length = min(length, duration) if duration > 0 else length
    out = []
    for center in np.argsort(smooth)[::-1]:
        if len(out) >= int(count):
            break
        start = max(0.0, min(float(center) - length * .4, duration - length))
        end = min(duration, start + length)
        if end - start < 1.0:
            continue
        if any(min(end, c['end']) - max(start, c['start']) > 0 for c in out):
            continue
        parts = {'motion': scan.get("motion") or [], 'audio jump': scan.get("loud") or [],
                 'shouting': scan.get("shout") or [], 'cuts': scan.get("cuts") or [],
                 'volume spike': scan.get("spike") or [], 'chat spike': scan.get("chat") or [],
                 'game event': scan.get("game") or []}
        c_lo, c_hi = max(0, int(center) - 3), int(center) + 4
        top = sorted(parts, key=lambda k: -(sum(parts[k][c_lo:c_hi]) / max(1, len(parts[k][c_lo:c_hi])) if parts[k] else 0))[:2]
        game_types = scan.get("game_types") or []
        ev_here = next((game_types[s] for s in range(max(0, int(start)), min(len(game_types), int(end)))
                        if game_types[s]), "")
        reason = 'Peak: ' + ' + '.join(top) + (f" · {ev_here}" if ev_here else "")
        feats = window_features(scan, start, end)
        out.append({'start': round(start, 2), 'end': round(end, 2),
                    'score': round(100.0 * float(smooth[center]) / max(peak, 1e-6), 1),
                    'title': f"Highlight {len(out) + 1}", 'hook': '', 'description': '',
                    'hashtags': [], 'highlight_words': [],
                    'reason': reason, 'kind': 'other', 'game_event': ev_here,
                    'features': {'loud': feats['loud'], 'shout': feats['shout'],
                                 'spike': feats['spike'], 'motion': feats['motion'],
                                 'cuts': feats['cuts'], 'chat': feats['chat'],
                                 'game': feats['game'], 'words': feats['words'], 'llm': None}})
    out.sort(key=lambda c: -c['score'])
    return out
