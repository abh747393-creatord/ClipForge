"""Caption style preview: one still frame with the chosen caption style
burned in, through the exact same path the real render uses
(captions.build_ass + layouts.text_positions + layouts.render_graph +
ffmpeg ass filter with the bundled fonts).

Used by POST /api/caption-preview so the UI can show how captions will
look BEFORE a full clip is rendered. With a job frame the preview runs
the real layout graph over that frame; without one it burns the captions
onto a generated placeholder frame at output size.
"""
import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import captions, config, layouts, media

WORD_SECONDS = 0.4        # sample-word timing for the preview
DEFAULT_TEXT = "THIS IS A TEST CAPTION"
DEFAULT_HOOK = "YOUR HOOK HERE"
DEFAULT_CAM_FX = "unsharp=5:5:0.8:5:5:0.0"   # same default render.py passes

_cache: dict = {}
_CACHE_MAX = 24


def _rect_px(rect, src_w, src_h):
    """Normalised {x, y, w, h} (0..1) -> source pixels (even, clamped), or None.
    Same conversion render.py uses."""
    if not isinstance(rect, dict):
        return None
    try:
        x, y = float(rect["x"]), float(rect["y"])
        w, h = float(rect["w"]), float(rect["h"])
    except (KeyError, TypeError, ValueError):
        return None
    px = max(0, min(src_w - 2, int(x * src_w))) // 2 * 2
    py = max(0, min(src_h - 2, int(y * src_h))) // 2 * 2
    pw = max(2, min(src_w - px, int(w * src_w))) // 2 * 2
    ph = max(2, min(src_h - py, int(h * src_h))) // 2 * 2
    if pw < 2 or ph < 2:
        return None
    return (px, py, pw, ph)


def _sample_words(text):
    """Word dicts for the sample caption line, WORD_SECONDS apart."""
    words = [w for w in str(text or "").split() if w.strip()]
    if not words:
        words = DEFAULT_TEXT.split()
    return [{"start": i * WORD_SECONDS, "end": (i + 1) * WORD_SECONDS, "text": w}
            for i, w in enumerate(words)]


def _placeholder_frame(width, height, dest):
    """A stand-in frame at output size (no job yet): dark gradient with a
    facecam zone on top and a gameplay zone below, so caption placement
    reads like a real clip."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), (24, 26, 38))
    d = ImageDraw.Draw(img)
    for y in range(height):  # vertical gradient, dark blue-grey
        t = y / max(1, height - 1)
        d.line([(0, y), (width, y)], fill=(int(24 + 30 * t), int(26 + 34 * t), int(38 + 52 * t)))
    # Facecam zone (top ~32%, like the stack layout).
    cam_h = int(height * 0.32)
    d.rectangle([0, 0, width, cam_h], fill=(52, 58, 82))
    cx, cy, r = width // 2, cam_h // 2, int(min(width, cam_h) * 0.22)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(140, 150, 180))          # head
    d.ellipse([cx - int(r * 1.7), cy + int(r * 0.7), cx + int(r * 1.7), cam_h], fill=(110, 120, 155))  # shoulders
    # Gameplay zone: a few "world" shapes so the frame isn't blank.
    gy = cam_h
    d.rectangle([0, gy, width, height], fill=(34, 52, 44))
    d.polygon([(0, height), (width * 0.35, gy + (height - gy) * 0.45), (width * 0.55, height)], fill=(44, 66, 54))
    d.polygon([(width * 0.45, height), (width * 0.8, gy + (height - gy) * 0.3), (width, height)], fill=(40, 60, 50))
    d.ellipse([width * 0.72, gy + (height - gy) * 0.08, width * 0.88, gy + (height - gy) * 0.24], fill=(210, 190, 120))
    img.save(dest)
    return dest


def render_preview(style="bounce", anim="highlight", text=None, hook=None,
                   layout="stack", aspect="vertical", top=0.32, caption_y=1450,
                   captions_on=True, hook_on=True,
                   frame_path=None, src_w=0, src_h=0, facecam=None, game=None,
                   highlight_words=None, caption_custom=None, hook_style="box"):
    """Render one preview frame (JPEG bytes) with the chosen caption style.

    frame_path: optional still from a job's source; when given, the real
    layout graph runs over it (src_w/src_h = source size, facecam/game =
    normalised rects). Otherwise a placeholder frame at output size is used.
    """
    style = str(style or "bounce")
    if style not in captions.STYLES:
        style = "bounce"
    anim = str(anim or "highlight")
    if anim not in captions.ANIMS:
        anim = "highlight"
    layout = str(layout or "stack")
    text = str(text or DEFAULT_TEXT)
    hook = str(hook if hook is not None else DEFAULT_HOOK)
    top = float(top or 0.32)
    caption_y = int(caption_y or 1450)

    cache_key = hashlib.sha1(repr((
        style, anim, text, hook, layout, aspect, top, caption_y,
        bool(captions_on), bool(hook_on), str(frame_path or ""),
        int(src_w or 0), int(src_h or 0), repr(facecam), repr(game),
        tuple(highlight_words or []), repr(caption_custom or {}), hook_style,
    )).encode()).hexdigest()
    if cache_key in _cache:
        return _cache[cache_key]

    width, height, _fps = layouts.output_settings({"aspect": aspect})
    pos = layouts.text_positions(layout, top, caption_y, width, height)
    words = _sample_words(text) if captions_on else []
    dur = (len(words) * WORD_SECONDS + 0.6) if words else 1.6
    ass = captions.build_ass(
        words, dur,
        hook if hook_on else "",
        style, anim,
        highlight_words or [],
        pos["hook_y"], pos["caption_y"], width, height,
        hook_enabled=bool(hook_on),
        style_override=caption_custom or None,
        hook_style=str(hook_style or "box"))
    # Frame time: mid-way through a middle word, so one word shows in its
    # active (highlighted) state exactly as it would mid-clip.
    if words:
        idx = min(2, len(words) - 1)
        at = idx * WORD_SECONDS + WORD_SECONDS / 2
    else:
        at = 0.3

    work = Path(tempfile.mkdtemp(prefix="cfp"))
    try:
        (work / "preview.ass").write_text(ass, encoding="utf-8")
        (work / "f").mkdir()
        for fnt in Path(config.FONTS_DIR).glob("*.ttf"):
            shutil.copyfile(fnt, work / "f" / fnt.name)

        if frame_path and Path(frame_path).is_file():
            bg = Path(frame_path)
            # Rects are normalised; convert with the FRAME IMAGE's real size
            # (job frames are extracted at 960px wide, not source size).
            try:
                from PIL import Image
                with Image.open(bg) as _im:
                    iw, ih = _im.size
            except Exception:  # noqa: BLE001
                iw, ih = int(src_w or 0), int(src_h or 0)
            graph = layouts.render_graph(
                layout,
                (_rect_px(facecam, iw, ih),
                 _rect_px(game, iw, ih)),
                top, width, height,
                cam_fx=DEFAULT_CAM_FX, src_w=iw, src_h=ih)
            chain = f"{graph};[base]ass=preview.ass:fontsdir=f[v]"
        else:
            bg = _placeholder_frame(width, height, work / "bg.png")
            chain = "[0:v]ass=preview.ass:fontsdir=f[v]"

        cmd = ["ffmpeg", "-y", "-v", "error", "-nostdin",
               "-loop", "1", "-framerate", "30", "-i", str(bg),
               "-filter_complex", chain,
               "-map", "[v]", "-ss", f"{at:.2f}", "-frames:v", "1",
               "-q:v", "3", "preview.jpg"]
        proc = subprocess.run(cmd, cwd=work, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              creationflags=getattr(media, "NO_WINDOW", 0),
                              timeout=120)
        out = work / "preview.jpg"
        if proc.returncode != 0 or not out.is_file():
            raise RuntimeError(f"Caption preview failed: {(proc.stderr or '')[-800:]}")
        data = out.read_bytes()
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[cache_key] = data
    return data
