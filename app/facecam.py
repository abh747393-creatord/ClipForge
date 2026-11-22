"""Where the facecam is — by measurement only.

A gaming stream is a game with a small webcam picture in one corner. The
webcam is found by the one thing it always has and a game almost never has:
a straight border that stays in the same place in every frame. A second
test tells it apart from the game's own panels (which have straight
borders too): the picture inside a webcam is almost still, while the game
behind a panel keeps moving.

Method (adapted from PeakCut's studio/facecam.py): sample ~24 grey frames
at 320x180 spread over the video, build per-column / per-row median edge
profiles, enumerate candidate rectangles from the strong border lines,
keep those that pass the stillness test, and prefer corner rectangles.

No AI or vision model is involved — measurement only. detect() never
raises; any failure returns None.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

try:
    import numpy as np
except Exception:  # noqa: BLE001 - without numpy there is simply no detection
    np = None

W, H = 320, 180            # the picture is looked at small: only the lines matter
MAX_FRAMES = 24            # frames spread through the video
EDGE = 2.2                 # how much brighter than the average line a border must be
STILL_FRAC = 0.35          # how much of the game's own movement a webcam picture may have
STILL_MIN = 6.0            # and the floor, for a game that itself is almost still
STILL_SHARE = 0.30         # at most this much of a webcam picture may be moving
ASPECT_MIN, ASPECT_MAX = 0.5, 2.0    # candidate width/height in pixels
AREA_MIN, AREA_MAX = 0.02, 0.40      # candidate area as a fraction of the frame
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _frames(src, duration, count=MAX_FRAMES):
    """~count grey frames at 320x180, spread over the video (first/last 2s skipped)."""
    dur = float(duration or 0.0)
    if dur <= 0:
        return np.zeros((0, H, W), dtype=np.uint8)
    start = 2.0 if dur > 6.0 else 0.0
    span = max(1.0, dur - 2 * start)
    rate = max(count / span, 0.02)
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{span:.2f}", "-i", str(src),
             "-vf", f"fps={rate:.4f},scale={W}:{H},format=gray",
             "-frames:v", str(count), "-f", "rawvideo", "-"],
            capture_output=True, timeout=180, creationflags=_NO_WINDOW)
    except Exception:  # noqa: BLE001 - no ffmpeg or unreadable file: no facecam, not a failure
        return np.zeros((0, H, W), dtype=np.uint8)
    raw = proc.stdout or b""
    size = W * H
    n = len(raw) // size
    if n < 4:
        return np.zeros((0, H, W), dtype=np.uint8)
    return np.frombuffer(raw[:n * size], dtype=np.uint8).reshape(n, H, W)


def _profiles(frames):
    """(vertical lines, horizontal lines): how much changes at each column/row.

    The median over frames is used, so one busy moment cannot invent a line."""
    if len(frames) < 4:
        return np.zeros(W), np.zeros(H)
    f = frames.astype(np.int16)
    gx = np.median(np.abs(np.diff(f, axis=2)).sum(axis=1), axis=0)   # change across each column boundary
    gy = np.median(np.abs(np.diff(f, axis=1)).sum(axis=2), axis=0)   # change across each row boundary
    return gx, gy


def _peaks(profile, limit):
    """The lines that stand out: [(where, strength)] strongest first."""
    if len(profile) < 3:
        return []
    med = float(np.median(profile)) or 1.0
    out = []
    for i in range(1, len(profile) - 1):
        if profile[i] >= profile[i - 1] and profile[i] > profile[i + 1] and profile[i] > EDGE * med:
            out.append((i, float(profile[i] / med)))
    return sorted(out, key=lambda p: -p[1])[:limit]


def _plausible(x0, y0, x1, y1, still, light, game):
    """Could this rectangle be a webcam picture? Returns its median motion, or None."""
    bw, bh = (x1 - x0) / W, (y1 - y0) / H
    if not (AREA_MIN <= bw * bh <= AREA_MAX):
        return None
    aspect = (bw * W) / max(1e-6, bh * H)
    if not (ASPECT_MIN <= aspect <= ASPECT_MAX):
        return None
    a, b, c, d = int(x0), int(y0), int(x1), int(y1)
    inside = still[b:d, a:c]
    if not inside.size:
        return None
    cut = max(STILL_MIN, STILL_FRAC * game)
    moving = float(np.median(inside))
    if moving > cut:
        return None                    # it changes with the game: it is the game
    if float((inside > cut).mean()) > STILL_SHARE:
        return None                    # a good part of it moves: the game is showing through
    if float(light[b:d, a:c].mean()) < 0.30 * float(light.mean()):
        return None                    # a black hole, not a picture
    return moving


def _corner_bonus(x0, y0, x1, y1):
    """1.0 for a corner rectangle, 0.5 for an edge one, 0.0 floating."""
    near = 0.02
    left, right = x0 <= W * near, x1 >= W * (1 - near)
    top, bottom = y0 <= H * near, y1 >= H * (1 - near)
    if (left or right) and (top or bottom):
        return 1.0
    if left or right or top or bottom:
        return 0.5
    return 0.0


def _candidates(vpeaks, hpeaks, still, light):
    """Every plausible rectangle the strong lines can make, best first:
    corner preference, then weakest-border strength, then size."""
    game = float(np.median(still))
    xs = sorted({0.0, float(W)} | {float(x) for x, _ in vpeaks[:10]})
    ys = sorted({0.0, float(H)} | {float(y) for y, _ in hpeaks[:10]})
    vs = {float(x): s for x, s in vpeaks}
    hs = {float(y): s for y, s in hpeaks}
    out = []
    for i, x0 in enumerate(xs):
        wx = [vs[v] for v in (x0,) if v in vs]
        for x1 in xs[i + 1:]:
            wscore = wx + [vs[v] for v in (x1,) if v in vs]
            for j, y0 in enumerate(ys):
                hy0 = [hs[v] for v in (y0,) if v in hs]
                for y1 in ys[j + 1:]:
                    score = wscore + hy0 + [hs[v] for v in (y1,) if v in hs]
                    if len(score) < 2:
                        continue       # a box needs two borders of its own, or it is just the frame
                    moving = _plausible(x0, y0, x1, y1, still, light, game)
                    if moving is None:
                        continue
                    key = (_corner_bonus(x0, y0, x1, y1), min(score), (x1 - x0) * (y1 - y0))
                    out.append({"key": key, "x": x0, "y": y0, "x1": x1, "y1": y1,
                                "score": round(min(score), 2), "moving": moving, "game": game})
    return sorted(out, key=lambda c: c["key"], reverse=True)


# ---------------------------------------------------------------- face tracks
# YuNet face detection + tracking, ported from stream-clips frame.py. Used by
# the "auto" look (small live face -> stack, big face -> follow, several
# faces -> blur) and the "follow" layout (a 9:16 window gliding after a face).
SAMPLE_W = 640
SAMPLE_FPS = 2
MAX_SAMPLES = 90
BIG_FACE = 0.13  # face wider than this share of frame width = talking head


def _sample_frames(src, start, length, src_w, src_h):
    """Low-res BGR frames from [start, start+length), plus the scale factor."""
    if np is None or not src_w or not src_h:
        return np.zeros((0, 2, 2, 3), np.uint8), 1.0
    h = max(2, int(src_h * SAMPLE_W / src_w) // 2 * 2)
    try:
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{length:.3f}",
             "-i", str(src), "-an", "-vf", f"fps={SAMPLE_FPS},scale={SAMPLE_W}:{h}",
             "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
            capture_output=True, timeout=600, creationflags=_NO_WINDOW)
    except Exception:  # noqa: BLE001
        return np.zeros((0, h, SAMPLE_W, 3), np.uint8), src_w / SAMPLE_W
    raw = proc.stdout or b""
    size = SAMPLE_W * h * 3
    n = len(raw) // size
    if not n:
        return np.zeros((0, h, SAMPLE_W, 3), np.uint8), src_w / SAMPLE_W
    frames = np.frombuffer(raw[: n * size], np.uint8).reshape(n, h, SAMPLE_W, 3)
    if n > MAX_SAMPLES:
        frames = frames[np.linspace(0, n - 1, MAX_SAMPLES).astype(int)]
    return frames, src_w / SAMPLE_W


def _faces_in(frames):
    """[(frame_index, x, y, w, h)] in sample pixels. YuNet first, Haar fallback."""
    if not len(frames):
        return []
    try:
        import cv2
    except Exception:  # noqa: BLE001
        return []
    try:
        cv2.setLogLevel(2)
    except AttributeError:
        pass
    h, w = frames.shape[1:3]
    out = []
    from . import config
    model = config.BASE_DIR / "models" / "face_yunet.onnx"
    if model.exists() and hasattr(cv2, "FaceDetectorYN"):
        det = cv2.FaceDetectorYN.create(str(model), "", (w, h), 0.65, 0.3, 50)
        for i, f in enumerate(frames):
            _, found = det.detect(np.ascontiguousarray(f))
            for row in ([] if found is None else found):
                x, y, fw, fh = [float(v) for v in row[:4]]
                if fw > 6 and fh > 6:
                    out.append((i, x, y, fw, fh))
    else:
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        for i, f in enumerate(frames):
            gray = cv2.equalizeHist(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY))
            for x, y, fw, fh in cascade.detectMultiScale(gray, 1.1, 5, minSize=(14, 14)):
                out.append((i, float(x), float(y), float(fw), float(fh)))
    return out


def face_tracks(src, start, length, src_w, src_h):
    """Face tracks over [start, start+length): [{box, presence, size, alive,
    items}] in SAMPLE pixels (box = median x,y,w,h; size = w / frame width;
    alive = the patch actually moves, so a static avatar/logo doesn't count).
    Returns (tracks, frames, scale). Never raises."""
    try:
        frames, scale = _sample_frames(src, start, length, src_w, src_h)
        if not len(frames):
            return [], frames, scale
        dets = _faces_in(frames)
        if not dets:
            return [], frames, scale
        n, h, w = len(frames), frames.shape[1], frames.shape[2]
        groups = []
        for d in sorted(dets):
            cx, cy = d[1] + d[3] / 2, d[2] + d[4] / 2
            for g in groups:
                if abs(cx - g["cx"]) < 0.07 * w and abs(cy - g["cy"]) < 0.10 * h:
                    g["items"].append(d)
                    k = len(g["items"])
                    g["cx"] += (cx - g["cx"]) / k
                    g["cy"] += (cy - g["cy"]) / k
                    break
            else:
                groups.append({"cx": cx, "cy": cy, "items": [d]})
        gray = frames.mean(axis=3)
        out = []
        for g in groups:
            idx = sorted({d[0] for d in g["items"]})
            box = np.median(np.array([d[1:] for d in g["items"]]), axis=0)
            x, y, bw, bh = [int(v) for v in box]
            patch = gray[:, max(0, y):y + max(2, bh), max(0, x):x + max(2, bw)]
            motion = float(np.abs(np.diff(patch, axis=0)).mean()) if len(patch) > 1 else 0.0
            out.append({"box": tuple(float(v) for v in box), "presence": len(idx) / n,
                        "size": float(box[2] / w), "alive": motion > 1.2, "items": g["items"]})
        return sorted(out, key=lambda t: -t["presence"]), frames, scale
    except Exception:  # noqa: BLE001 - tracking failure = no tracks, never an error
        return [], np.zeros((0, 2, 2, 3), np.uint8), 1.0


def cam_box_from_face(frames, face):
    """The facecam overlay around `face` (sample pixels), found from edges
    that stay put across frames; falls back to a box sized from the face.
    Returns ((x, y, w, h), how). Ported from stream-clips frame.cam_box()."""
    import cv2
    n, H, W = frames.shape[:3]
    fx, fy, fw, fh = face
    persist = np.zeros((H, W), np.float32)
    for f in frames:
        persist += cv2.Canny(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), 60, 150) > 0
    persist /= max(1, n)
    persist = cv2.dilate(persist, np.ones((3, 3), np.uint8))
    xs = slice(int(max(0, fx - fw)), int(min(W, fx + 2 * fw)))
    ys = slice(int(max(0, fy - 0.5 * fh)), int(min(H, fy + 2 * fh)))
    rows = persist[:, xs].mean(axis=1)
    cols = persist[ys, :].mean(axis=0)

    def strongest(scores, a, b, edge):
        a, b = int(max(0, a)), int(min(len(scores), b))
        if b - a < 2:
            return edge
        seg = scores[a:b]
        k = int(np.argmax(seg))
        return a + k if seg[k] >= 0.6 else edge

    top = strongest(rows, fy - 4.0 * fh, fy - 0.3 * fh, 0 if fy - 4.0 * fh <= 0 else None)
    bottom = strongest(rows, fy + 1.4 * fh, fy + 5.0 * fh, H if fy + 5.0 * fh >= H else None)
    left = strongest(cols, fx - 6.0 * fw, fx - 0.3 * fw, 0 if fx - 6.0 * fw <= 0 else None)
    right = strongest(cols, fx + 1.3 * fw, fx + 7.0 * fw, W if fx + 7.0 * fw >= W else None)
    if None not in (top, bottom, left, right):
        bw, bh = right - left, bottom - top
        if (2.0 * fw <= bw <= 10 * fw and 1.8 * fh <= bh <= 7 * fh and bw <= 0.65 * W
                and bh <= 0.75 * H and 0.6 <= bw / bh <= 2.6):
            return (float(left), float(top), float(bw), float(bh)), "overlay edges"
    bw = min(4.5 * fw, 0.6 * W)
    bh = min(bw * 0.75, 0.7 * H)
    x0 = min(max(fx + fw / 2 - bw / 2, 0), W - bw)
    y0 = min(max(fy + fh / 2 - 0.42 * bh, 0), H - bh)
    return (x0, y0, bw, bh), "face size"


def auto_look(src, start, length, src_w, src_h, forced_cam=None):
    """Pick the look for one clip (stream-clips frame.plan()):
      small live face  -> "stack" (facecam over gameplay)
      one big face     -> "follow" (9:16 window glides after the face)
      several faces    -> "blur" (probably thumbnails / a panel)
      no usable face   -> "blur"
    Returns {"layout", "cam" (normalised rect or None), "track", "frames",
    "scale", "why"}. Never raises."""
    out = {"layout": "blur", "cam": forced_cam, "track": None, "frames": None,
           "scale": 1.0, "why": "no facecam found"}
    try:
        if forced_cam:
            out.update(layout="stack", why="your selected facecam area")
            return out
        tracks, frames, scale = face_tracks(src, start, length, src_w, src_h)
        out["frames"], out["scale"] = frames, scale
        live = [t for t in tracks if t["alive"] and t["presence"] >= 0.3]
        big = [t for t in live if t["size"] >= BIG_FACE]
        if big and len(big) == 1 and big[0]["presence"] >= 0.5:
            out.update(layout="follow", track=big[0], why="one big face: follow it")
            return out
        if len(big) >= 2:
            out["why"] = "more than one big face on screen"
            return out
        small = [t for t in live if t["size"] < BIG_FACE]
        if small:
            if len([s for s in small if s["presence"] >= 0.5]) > 2:
                out["why"] = "several faces, probably thumbnails"
                return out
            t = small[0]
            box, how = cam_box_from_face(frames, t["box"])
            cam = {"x": round(box[0] * scale / src_w, 4), "y": round(box[1] * scale / src_h, 4),
                   "w": round(box[2] * scale / src_w, 4), "h": round(box[3] * scale / src_h, 4)}
            out.update(layout="stack", cam=cam, track=t, why=f"facecam found by {how}")
            return out
        return out
    except Exception:  # noqa: BLE001
        return out


def follow_keys(track, n_frames, scale, src_w, src_h, out_w=1080, out_h=1920):
    """Eased crop-x keyframes for the follow layout: [(t_sec, x_px), ...]
    in SOURCE pixels, plus the crop width. Ported from stream-clips
    frame.crop_graph()."""
    cw = max(2, int(min(src_w, src_h * out_w / out_h)) // 2 * 2)
    limit = max(0, src_w - cw)
    centre = np.full(max(1, n_frames), np.nan)
    for i, x, _y, w, _h in track["items"]:
        if 0 <= i < len(centre):
            centre[i] = (x + w / 2) * scale
    known = np.where(~np.isnan(centre))[0]
    if not len(known):
        return [(0.0, limit / 2)], cw
    centre = np.interp(np.arange(len(centre)), known, centre[known])
    k = 5
    smooth = np.convolve(np.pad(centre, k // 2, mode="edge"), np.ones(k) / k, mode="valid")
    xs = np.clip(smooth - cw / 2, 0, limit)
    keys = [(0.0, float(xs[0]))]
    for i in range(SAMPLE_FPS, len(xs), SAMPLE_FPS):
        if abs(xs[i] - keys[-1][1]) > 0.04 * src_w:
            keys.append((i / SAMPLE_FPS, float(xs[i])))
    return keys[:40], cw


def detect(src, duration, src_w, src_h):
    """The facecam box as {"x", "y", "w", "h", "conf"} normalised to 0..1, or None.

    conf is "high" when both the border strength and the stillness are
    strong, else "low". Measurement only (border profiles + stillness);
    no AI/vision model. Never raises.
    """
    try:
        if np is None or not src_w or not src_h:
            return None
        frames = _frames(Path(src), duration)
        if len(frames) < 4:
            return None
        still = frames.astype(np.float32).std(axis=0)      # per-pixel temporal std
        light = frames.astype(np.float32).mean(axis=0)
        vx, hy = _profiles(frames)
        vpeaks, hpeaks = _peaks(vx, 40), _peaks(hy, 40)
        if not vpeaks or not hpeaks:
            return None
        found = _candidates(vpeaks, hpeaks, still, light)
        if not found:
            return None
        best = found[0]
        strong_border = best["score"] >= 3.0
        strong_still = best["moving"] <= max(3.0, 0.25 * best["game"])
        conf = "high" if (strong_border and strong_still) else "low"
        return {"x": round(best["x"] / W, 4), "y": round(best["y"] / H, 4),
                "w": round((best["x1"] - best["x"]) / W, 4), "h": round((best["y1"] - best["y"]) / H, 4),
                "conf": conf}
    except Exception:  # noqa: BLE001 - detection failure means "no facecam", never an error
        return None
