"""Game-event detection: watch the GAME, not just the audio.

Inspired by Wanted (gta6clipmaker.com): find WASTED / BUSTED screens,
explosions, crashes and chases visually, so moments are found even when
nobody is talking. Implemented with OpenCV heuristics over low-res frames
sampled at 2 fps:

  wasted / busted   the screen darkens hard, big text appears centre-frame
                    (edge density in the centre band spikes); a red tint
                    reads as WASTED, a blue tint as BUSTED. If tesseract
                    OCR is installed it confirms the actual word;
                    otherwise this stays a heuristic with capped
                    confidence (see ocr_available()).
  explosion         a brightness flash plus a motion spike in the same
                    second (a boom lights the screen and shakes it).
  crash             a motion spike that stops dead within ~1.5 s
                    (vehicle hits something and everything halts).
  chase             sustained high motion for 3+ seconds (pursuit /
                    frantic action; weakest signal, labelled honestly).

scan() returns {"events": [{start, end, type, confidence}],
"per_second": [0..1 strength], "types": [best type per second]}.
Never raises: any failure returns empty events.
"""
import subprocess

import numpy as np

try:
    import cv2
except Exception:  # pragma: no cover - cv2 optional at runtime
    cv2 = None

FPS = 2
W, H = 320, 180
MAX_FRAMES = 7200  # 1 hour at 2 fps; longer sources are sampled thinner


def ocr_available() -> bool:
    """True when tesseract OCR can confirm WASTED/BUSTED text."""
    try:
        import shutil
        import pytesseract  # noqa: F401
        return shutil.which("tesseract") is not None
    except Exception:
        return False


def _frames(source, duration):
    """BGR frames at W x H, ~FPS per second (thinned for very long sources)."""
    if cv2 is None or duration <= 0:
        return np.zeros((0, H, W, 3), np.uint8)
    fps = FPS
    if duration * fps > MAX_FRAMES:
        fps = MAX_FRAMES / duration
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(source), "-an",
           "-vf", f"fps={fps:.4f},scale={W}:{H}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    try:
        raw = subprocess.run(cmd, capture_output=True, timeout=1800).stdout
    except Exception:
        return np.zeros((0, H, W, 3), np.uint8)
    size = W * H * 3
    n = len(raw) // size
    if not n:
        return np.zeros((0, H, W, 3), np.uint8)
    return np.frombuffer(raw[: n * size], np.uint8).reshape(n, H, W, 3), fps


def _features(frames):
    """Per-frame feature arrays: bright, red, blue, center_edge, motion, flash."""
    n = len(frames)
    f = frames.astype(np.float32)
    b, g, r = f[..., 0], f[..., 1], f[..., 2]
    bright = f.mean(axis=(1, 2, 3))
    red = r.mean(axis=(1, 2)) - (g.mean(axis=(1, 2)) + b.mean(axis=(1, 2))) / 2
    blue = b.mean(axis=(1, 2)) - (r.mean(axis=(1, 2)) + g.mean(axis=(1, 2))) / 2
    # Centre band edge density: big centred text (WASTED/BUSTED) lights this up.
    y0, y1, x0, x1 = int(H * 0.28), int(H * 0.72), int(W * 0.15), int(W * 0.85)
    edge = np.zeros(n)
    edge_all = np.zeros(n)
    prev_gray = None
    motion = np.zeros(n)
    for i in range(n):
        gray = cv2.cvtColor(frames[i], cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 60, 150)
        edge[i] = float((edges[y0:y1, x0:x1] > 0).mean())
        edge_all[i] = float((edges > 0).mean())
        if prev_gray is not None:
            motion[i] = float(np.abs(gray.astype(np.int16) - prev_gray.astype(np.int16)).mean())
        prev_gray = gray
    flash = np.zeros(n)
    flash[1:] = bright[1:] - bright[:-1]
    return {"bright": bright, "red": red, "blue": blue, "edge": edge,
            "edge_all": edge_all, "motion": motion, "flash": flash}


def _rolling_median(x, win):
    if not len(x):
        return x
    half = max(1, win // 2)
    pad = np.pad(x, half, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(pad, 2 * half + 1)
    return np.median(windows, axis=1)[: len(x)]


def _ocr_word(frame):
    """OCR the centre band for WASTED/BUSTED; '' when OCR is unavailable."""
    if not ocr_available():
        return ""
    try:
        import pytesseract
        y0, y1, x0, x1 = int(H * 0.28), int(H * 0.72), int(W * 0.15), int(W * 0.85)
        band = frame[y0:y1, x0:x1]
        band = cv2.resize(band, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        text = pytesseract.image_to_string(band).upper()
        for word in ("WASTED", "BUSTED"):
            if word in text:
                return word.lower()
    except Exception:
        pass
    return ""


def scan(source, duration, report=lambda m: None) -> dict:
    """Detect game events in `source`. Never raises."""
    duration = float(duration or 0)
    n_sec = max(1, int(np.ceil(duration)))
    empty = {"events": [], "per_second": [0.0] * n_sec, "types": [""] * n_sec,
             "ocr": ocr_available()}
    try:
        if cv2 is None or duration < 2:
            return empty
        frames, fps = _frames(source, duration)
        if len(frames) < 4:
            return empty
        report("Watching the game for events")
        ft = _features(frames)
        t = np.arange(len(frames)) / fps

        base_bright = _rolling_median(ft["bright"], int(30 * fps))
        dark = ft["bright"] < 0.5 * np.maximum(base_bright, 1.0)
        # Centred text: centre-band edges denser than the frame's own edges,
        # or a strong red/blue tint, or plain dense centre structure.
        centred = (ft["edge"] > 1.25 * np.maximum(ft["edge_all"], 1e-6)) \
            | (ft["red"] > 8.0) | (ft["blue"] > 8.0) | (ft["edge"] > 0.03)
        death = dark & centred

        motion_hi = ft["motion"] > max(6.0, float(np.percentile(ft["motion"], 90)))
        flash_hi = ft["flash"] > 18.0

        events = []

        def runs(mask, min_len):
            """[(i0, i1)] index runs of True lasting at least min_len frames."""
            out, start = [], None
            for i, v in enumerate(mask):
                if v and start is None:
                    start = i
                elif not v and start is not None:
                    if i - start >= min_len:
                        out.append((start, i - 1))
                    start = None
            if start is not None and len(mask) - start >= min_len:
                out.append((start, len(mask) - 1))
            return out

        # WASTED / BUSTED screens (sustained dark + centred text).
        for i0, i1 in runs(death, max(2, int(1.0 * fps))):
            seg = slice(i0, i1 + 1)
            kind, conf = "death_screen", 0.55
            word = _ocr_word(frames[(i0 + i1) // 2])
            if word:
                kind, conf = word, 0.95
            elif float(ft["red"][seg].mean()) > 4.0:
                kind, conf = "wasted", 0.7
            elif float(ft["blue"][seg].mean()) > 4.0:
                kind, conf = "busted", 0.7
            events.append({"start": round(float(t[i0]), 2), "end": round(float(t[i1]) + 1 / fps, 2),
                           "type": kind, "confidence": conf})

        # Explosions: flash + motion together.
        boom = flash_hi & motion_hi
        for i0, i1 in runs(boom, 1):
            events.append({"start": round(float(t[i0]), 2), "end": round(float(t[i1]) + 1 / fps, 2),
                           "type": "explosion", "confidence": 0.65})

        # Crashes: motion spike then a dead stop.
        for i0, i1 in runs(motion_hi, 1):
            peak = float(ft["motion"][i0:i1 + 1].max())
            after = ft["motion"][i1 + 1: i1 + 1 + int(1.5 * fps)]
            if len(after) and float(after.max()) < 0.25 * peak:
                events.append({"start": round(float(t[i0]), 2), "end": round(float(t[i1]) + 1 / fps, 2),
                               "type": "crash", "confidence": 0.6})

        # Chases: sustained high motion.
        chase = ft["motion"] > max(4.0, float(np.percentile(ft["motion"], 70)))
        for i0, i1 in runs(chase, int(3.0 * fps)):
            events.append({"start": round(float(t[i0]), 2), "end": round(float(t[i1]) + 1 / fps, 2),
                           "type": "chase", "confidence": 0.45})

        # Per-second strength + best type (death screens weigh most).
        weights = {"wasted": 1.0, "busted": 1.0, "death_screen": 0.9,
                   "explosion": 0.85, "crash": 0.75, "chase": 0.4}
        per_second = np.zeros(n_sec)
        types = [""] * n_sec
        for ev in events:
            w = weights.get(ev["type"], 0.5) * ev["confidence"]
            for s in range(int(ev["start"]), min(n_sec, int(np.ceil(ev["end"])))):
                if w > per_second[s]:
                    per_second[s] = w
                    types[s] = ev["type"]
        events.sort(key=lambda e: e["start"])
        return {"events": events, "per_second": [float(v) for v in per_second],
                "types": types, "ocr": ocr_available()}
    except Exception:  # noqa: BLE001 - detection is best-effort, never a failed job
        return empty
