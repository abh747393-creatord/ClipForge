"""The title bar over a clip, drawn as a transparent PNG with Pillow.

Pillow (not libass) draws it because it can render colour emoji from the
system emoji font. Ported from stream-clips titlecard.py and adapted to
ClipForge's fonts. If no colour emoji font works, emoji are left out.
"""
import unicodedata
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import config

EMOJI_FONTS = [
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/System/Library/Fonts/Apple Color Emoji.ttc",
    "C:/Windows/Fonts/seguiemj.ttf",
]
BOLD_FONTS = [
    str(config.FONTS_DIR / "Poppins-ExtraBold.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def _is_emoji(ch):
    cp = ord(ch)
    return (0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or 0x2B00 <= cp <= 0x2BFF
            or cp in (0x200D, 0xFE0F, 0x20E3))


def _hex(c, alpha=255):
    c = str(c).lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), alpha)


def _first_font(paths, size):
    for p in paths:
        if p and Path(p).exists():
            try:
                return ImageFont.truetype(p, size), p
            except OSError:
                continue
    return ImageFont.load_default(size), None


class _Emoji:
    """Renders one emoji at a target size, or returns None if the system can't."""

    def __init__(self):
        self.font = None
        self.native = None
        for p in EMOJI_FONTS:
            if not Path(p).exists():
                continue
            for size in (109, 136, 160, 96, 64):  # bitmap emoji fonts accept fixed sizes
                try:
                    self.font = ImageFont.truetype(p, size)
                    self.native = size
                    break
                except OSError:
                    continue
            if self.font:
                break
        if self.font and self._render("\U0001F600", 64) is None:  # a smiley must come out in colour
            self.font = None

    def draw(self, ch, px):
        return self._render(ch, px) if self.font else None

    def _render(self, ch, px):
        try:
            tile = Image.new("RGBA", (self.native * 2, self.native * 2), (0, 0, 0, 0))
            ImageDraw.Draw(tile).text((self.native // 4, self.native // 4), ch, font=self.font,
                                      embedded_color=True)
            box = tile.getbbox()
            if not box:
                return None
            glyph = tile.crop(box)
            if ch == "\U0001F600":  # one-time colour test
                px_ = np.asarray(glyph).reshape(-1, 4).astype(np.int16)
                solid = px_[px_[:, 3] > 128, :3]
                if not len(solid) or float(np.abs(solid - solid.mean(axis=1, keepdims=True)).mean()) < 6:
                    return None  # no colour = the font has no colour emoji
            ratio = px / max(glyph.size)
            return glyph.resize((max(1, int(glyph.width * ratio)), max(1, int(glyph.height * ratio))),
                                Image.LANCZOS)
        except (OSError, ValueError):
            return None


def _tokens(text):
    """Split into words; each word into ('t', text) and ('e', emoji) pieces."""
    out = []
    for word in str(text).split():
        pieces, buf = [], ""
        for ch in word:
            if _is_emoji(ch):
                if buf:
                    pieces.append(("t", buf))
                    buf = ""
                if ch not in ("‍", "️"):
                    pieces.append(("e", ch))
            elif unicodedata.category(ch) != "Cf":
                buf += ch
        if buf:
            pieces.append(("t", buf))
        if pieces:
            out.append(pieces)
    return out


def render(text, path, width=1080, size=56, bg="FFFFFF", fg="111111"):
    """Write the title bar PNG. Returns (path, width, height). Raises
    ValueError on empty text (caller treats the bar as optional)."""
    font, _ = _first_font(BOLD_FONTS, size)
    emoji = _Emoji()
    em = int(size * 1.1)
    glyphs = {}

    def piece_width(kind, value):
        if kind == "t":
            return font.getlength(value)
        if value not in glyphs:
            glyphs[value] = emoji.draw(value, em)
        return em if glyphs[value] is not None else 0

    words = _tokens(text)
    space = font.getlength(" ")
    pad_x, pad_y, line_h = int(size * 0.6), int(size * 0.38), int(size * 1.28)
    limit = width - 2 * int(width * 0.11) - 2 * pad_x
    lines, cur, cur_w = [], [], 0.0
    for word in words:
        w = sum(piece_width(k, v) for k, v in word)
        if not w:
            continue
        extra = w + (space if cur else 0)
        if cur and cur_w + extra > limit:
            lines.append((cur, cur_w))
            cur, cur_w = [word], w
        else:
            cur.append(word)
            cur_w += extra
    if cur:
        lines.append((cur, cur_w))
    if not lines:
        raise ValueError("empty title")

    box_w = int(max(w for _, w in lines) + 2 * pad_x)
    box_h = int(len(lines) * line_h + 2 * pad_y)
    img = Image.new("RGBA", (box_w + 12, box_h + 12), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((8, 10, box_w + 8, box_h + 10), radius=int(size * 0.32), fill=(0, 0, 0, 70))
    draw.rounded_rectangle((0, 0, box_w, box_h), radius=int(size * 0.32), fill=_hex(bg))
    ink = _hex(fg)
    ascent = font.getmetrics()[0]
    for row, (line, line_w) in enumerate(lines):
        x = (box_w - line_w) / 2
        top = pad_y + row * line_h
        baseline = top + (line_h + ascent) / 2 - size * 0.14
        for wi, word in enumerate(line):
            if wi:
                x += space
            for kind, value in word:
                if kind == "t":
                    draw.text((x, baseline), value, font=font, fill=ink, anchor="ls")
                    x += font.getlength(value)
                elif glyphs.get(value) is not None:
                    g = glyphs[value]
                    img.alpha_composite(g, (int(x), int(top + (line_h - g.height) / 2)))
                    x += em
    img.save(path)
    return path, img.width, img.height


def stars_png(path, count=3, star_px=54):
    """Wanted-style star row as a transparent PNG (drawn, so no font needs
    the ★ glyph). Returns (path, width, height)."""
    count = max(1, min(5, int(count)))
    pad = 10
    w = count * star_px + (count + 1) * pad
    h = star_px + 2 * pad
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    import math
    for i in range(count):
        cx = pad + star_px / 2 + i * (star_px + pad)
        cy = h / 2
        r_out, r_in = star_px / 2, star_px / 4.6
        pts = []
        for k in range(10):
            r = r_out if k % 2 == 0 else r_in
            a = -math.pi / 2 + k * math.pi / 5
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        draw.polygon(pts, fill=(255, 255, 255, 235), outline=(20, 20, 20, 255), width=3)
    img.save(path)
    return path, w, h


def mission_card_png(path, text="MISSION COMPLETE", width=1080, size=64):
    """A GTA-style mission card: dark translucent band, gold text.
    Returns (path, width, height)."""
    font, _ = _first_font([str(config.FONTS_DIR / "ArchivoBlack-Regular.ttf")] + BOLD_FONTS, size)
    tw = font.getlength(text)
    pad_x, pad_y = 48, 26
    box_w = int(min(width - 40, tw + 2 * pad_x))
    box_h = int(size * 1.5 + 2 * pad_y)
    img = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, box_w, box_h), radius=14, fill=(8, 8, 8, 190))
    draw.rounded_rectangle((0, 0, box_w, box_h), radius=14, outline=(212, 175, 55, 255), width=3)
    draw.text((box_w / 2, box_h / 2), text, font=font, fill=(212, 175, 55, 255), anchor="mm")
    img.save(path)
    return path, box_w, box_h
