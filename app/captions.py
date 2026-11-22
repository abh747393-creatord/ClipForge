"""Word-timed burned captions: ASS generation, SRT/VTT sidecars.

Merged from PeakCut's studio/render.py (measured per-word placement, pop
animation, hook headline box) and NeonClipping's captions.py (style set,
grouping with lone-word rebalance, animation set).

Words everywhere are ClipForge Word dicts: {"start": float, "end": float,
"text": str} in seconds. `words_rel` means times relative to clip start.
For robustness the PeakCut shape ({"w", "s", "e"}) is also accepted and
normalised on entry.
"""
import re
import struct
from pathlib import Path

from . import config, roman

# ASS colours are RRGGBB hex strings, emitted as &H00<value> (PeakCut style).
STYLES = {
    # PeakCut values verbatim.
    "bounce": {"label": "Bounce", "font": "Poppins ExtraBold", "size": 132, "base": "FFFFFF", "active": "00F0FF",
               "strong": "FFFFFF", "outline": 7, "pop": 112, "upper": True, "per": 3},
    "hype": {"label": "Hype", "font": "Anton", "size": 152, "base": "FFFFFF", "active": "3C3CFF",
             "strong": "00D7FF", "outline": 7, "pop": 114, "upper": True, "per": 2},
    "clean": {"label": "Clean", "font": "Poppins ExtraBold", "size": 100, "base": "FFFFFF", "active": "6BD7FF",
              "strong": "FFFFFF", "outline": 5, "pop": 106, "upper": False, "per": 4},
    # Adapted from NeonClipping's styles.
    "gta": {"label": "GTA Neon", "font": "Poppins ExtraBold", "size": 72, "base": "43EFFF", "active": "FF45A8",
            "strong": "FF45A8", "outline": 4, "pop": 112, "upper": True, "per": 3},
    "bold": {"label": "Bold", "font": "Archivo Black", "size": 72, "base": "FFFFFF", "active": "CBF33D",
             "strong": "CBF33D", "outline": 6, "pop": 112, "upper": True, "per": 3},
    "hormozi": {"label": "Hormozi", "font": "Archivo Black", "size": 84, "base": "FFFFFF", "active": "FFFF00",
                "strong": "FFFF00", "outline": 7, "pop": 112, "upper": True, "per": 3},
    "mrbeast": {"label": "MrBeast", "font": "Anton", "size": 90, "base": "FFFF00", "active": "FF6600",
                "strong": "FF6600", "outline": 9, "pop": 112, "upper": True, "per": 2},
}

ANIMS = ("highlight", "pop_in", "one_by_one", "build_up", "full_line")

HOOK = {"font": "Poppins ExtraBold", "size": 96, "ink": "14100E", "box": "FFFFFF", "pad_x": 36, "pad_y": 22,
        "radius": 26, "max_w": 900}
FONT_FILES = {"Poppins ExtraBold": "Poppins-ExtraBold.ttf", "Anton": "Anton-Regular.ttf",
              "Archivo Black": "ArchivoBlack-Regular.ttf"}
CAP_MAX_W = 960   # captions never run wider than this at 1080 wide, whatever is said


def _norm(word):
    """One word in ClipForge shape, accepting the PeakCut {w, s, e} shape too."""
    try:
        return {"start": float(word.get("start", word.get("s", 0.0))),
                "end": float(word.get("end", word.get("e", 0.0))),
                "text": str(word.get("text", word.get("w", "")))}
    except (TypeError, ValueError, AttributeError):
        return {"start": 0.0, "end": 0.0, "text": ""}


def _clean(text):
    return re.sub(r"[\x00-\x1f\x7f]+", " ", re.sub(r"[{}\\]", "", str(text))).strip()


def _key(word):
    return re.sub(r"[^a-z0-9']", "", str(word).lower())


def _ts(t):
    """ASS timestamp H:MM:SS.cc."""
    t = max(0.0, float(t))
    h, m = int(t // 3600), int(t % 3600 // 60)
    return f"{h}:{m:02d}:{t % 60:05.2f}"


def _stamp(t, comma):
    """SRT (comma) or VTT (dot) timestamp HH:MM:SS.mmm."""
    ms = int(round(max(0.0, float(t)) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    sep = "," if comma else "."
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


class _Font:
    """Measures text exactly as the renderer will draw it, so every word can be
    placed by hand: the highlighted word can grow without pushing its
    neighbours, and the heading box fits its text. Ported from PeakCut.

    If the font file is missing or unreadable, widths fall back to a fair
    estimate (len * em * 0.62) so caption building never crashes.
    """
    _cache: dict = {}

    def __init__(self, name):
        self.name = name
        self.path = None
        self._pil = {}
        # Fallback metrics (used when the TTF cannot be parsed).
        self.upm, self.asc, self.desc, self.cap = 1000, 800, 200, 700
        self.ok = False
        try:
            path = Path(config.FONTS_DIR) / FONT_FILES.get(name, FONT_FILES["Poppins ExtraBold"])
            d = path.read_bytes()
            tables = {}
            for i in range(struct.unpack(">H", d[4:6])[0]):
                tag, _, off, _ = struct.unpack(">4sIII", d[12 + 16 * i: 28 + 16 * i])
                tables[tag] = off
            self.upm = struct.unpack(">H", d[tables[b"head"] + 18: tables[b"head"] + 20])[0]
            o = tables[b"OS/2"]
            self.asc, self.desc = struct.unpack(">HH", d[o + 74: o + 78])  # what the renderer sizes the font by
            self.cap = struct.unpack(">h", d[o + 88: o + 90])[0] or int(self.upm * 0.7)
            self.path = str(path)
            self.ok = True
        except Exception:  # noqa: BLE001 - missing font: estimate widths, never crash
            self.path = None

    @classmethod
    def get(cls, name):
        if name not in cls._cache:
            cls._cache[name] = cls(name)
        return cls._cache[name]

    def em(self, size):
        return size * self.upm / max(1, (self.asc + self.desc))

    def width(self, text, size):
        """Pixels wide at this ASS size."""
        if self.path:
            try:
                from PIL import ImageFont
                if "f" not in self._pil:
                    self._pil["f"] = ImageFont.truetype(self.path, 200)
                return self._pil["f"].getlength(text) * self.em(size) / 200
            except Exception:  # noqa: BLE001 - no measuring library: a fair estimate keeps clips rendering
                pass
        return len(text) * self.em(size) * 0.62

    def cap_shift(self, size):
        """How far below the anchor the middle of a capital letter sits when text is centred on a point."""
        return ((self.asc - self.desc) / 2 - self.cap / 2) * size / max(1, (self.asc + self.desc))


def group_words(words, per, max_chars=18, gap=0.45):
    """Caption groups of up to `per` words that follow the speech.

    PeakCut's grouping (break on word count, character budget, pause > gap,
    or sentence-final punctuation) plus NeonClipping's lone-trailing-word
    rebalance: a single last word is merged into the previous group when it
    fits, otherwise one word moves back so 4+1 becomes 3+2.
    """
    per = max(1, int(per or 3))
    words = [_norm(w) for w in (words or [])]
    words = [w for w in words if w["text"].strip()]
    out, cur = [], []
    for w in words:
        if cur and (len(cur) >= per
                    or sum(len(x["text"]) for x in cur) + len(w["text"]) > max_chars
                    or w["start"] - cur[-1]["end"] > gap
                    or cur[-1]["text"][-1:] in ".?!"):
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    # A lone trailing word reads badly: merge it into the previous group when
    # that stays within per+1 words, else move one word back (per+1 split).
    if len(out) > 1 and len(out[-1]) == 1:
        lone = out[-1][0]
        prev = out[-2]
        if lone["start"] - prev[-1]["end"] < gap:
            if len(prev) < per:
                prev.append(lone)
                out.pop()
            elif len(prev) >= per and len(prev) > 1:
                out[-1].insert(0, prev.pop())
    return out


def _hook_events(hook, dur, y, width, height):
    """The heading: dark text in a white rounded box that fits it, on one line
    or two balanced ones. Ported from PeakCut's _hook_events, scaled to the
    output frame (authored at 1080x1920)."""
    sx, sy = width / 1080.0, height / 1920.0
    f = _Font.get(HOOK["font"])
    size = max(24, int(round(HOOK["size"] * sy)))
    max_w = HOOK["max_w"] * sx
    words = hook.split()
    if not words:
        return []
    lines = [" ".join(words)]
    if f.width(lines[0], size) > max_w and len(words) > 1:  # two lines, split where they come out most even
        cut = min(range(1, len(words)),
                  key=lambda i: max(f.width(" ".join(words[:i]), size), f.width(" ".join(words[i:]), size)))
        lines = [" ".join(words[:cut]), " ".join(words[cut:])]
    widest = max(f.width(l, size) for l in lines)
    if widest > max_w:
        size = max(24, int(size * max_w / widest))
        widest = max(f.width(l, size) for l in lines)
    line_h = f.em(size) * 1.16
    pad_x, pad_y = HOOK["pad_x"] * sx, HOOK["pad_y"] * sy
    bw, bh = widest + 2 * pad_x, line_h * len(lines) + 2 * pad_y
    x0, y0 = (width - bw) / 2, y - bh / 2
    r = min(HOOK["radius"] * sx, bh / 2)
    x1, y1 = x0 + bw, y0 + bh
    n = lambda v: f"{v:.0f}"  # noqa: E731
    box = (f"m {n(x0 + r)} {n(y0)} l {n(x1 - r)} {n(y0)} b {n(x1)} {n(y0)} {n(x1)} {n(y0)} {n(x1)} {n(y0 + r)} "
           f"l {n(x1)} {n(y1 - r)} b {n(x1)} {n(y1)} {n(x1)} {n(y1)} {n(x1 - r)} {n(y1)} "
           f"l {n(x0 + r)} {n(y1)} b {n(x0)} {n(y1)} {n(x0)} {n(y1)} {n(x0)} {n(y1 - r)} "
           f"l {n(x0)} {n(y0 + r)} b {n(x0)} {n(y0)} {n(x0)} {n(y0)} {n(x0 + r)} {n(y0)}")
    span = f"{_ts(0)},{_ts(dur)}"
    ev = [f"Dialogue: 3,{span},Hook,,0,0,0,,{{\\an7\\pos(0,0)\\fad(160,0)\\c&H{HOOK['box']}&\\p1}}{box}{{\\p0}}"]
    for i, line in enumerate(lines):
        cy = y - line_h * (len(lines) - 1) / 2 + i * line_h - f.cap_shift(size)
        ev.append(f"Dialogue: 4,{span},Hook,,0,0,0,,{{\\an5\\pos({width // 2},{cy:.0f})\\fad(160,0)\\fs{size}}}{line}")
    return ev


def build_ass(words_rel, dur, hook, style_name, anim, highlight_words, hook_y, cap_y,
              width=1080, height=1920, hook_enabled=True):
    """A complete ASS document for one clip.

    words_rel are relative to clip start; dur is the clip length in seconds.
    hook_y / cap_y are authored in 1080x1920 space and scaled by height/1920.
    Animations: highlight (all words visible, spoken word highlighted),
    pop_in (words appear as spoken, with a pop), one_by_one (only the spoken
    word, centred), build_up (words accumulate), full_line (static line).
    """
    st = STYLES.get(style_name, STYLES["bounce"])
    if anim not in ANIMS:
        anim = "highlight"
    dur = max(0.0, float(dur or 0.0))
    width, height = int(width), int(height)
    sy = height / 1920.0
    cap_y_s = float(cap_y) * sy
    hook_y_s = float(hook_y) * sy
    cap_max_w = CAP_MAX_W * (width / 1080.0)
    base_size = max(20, int(round(st["size"] * sy)))
    hook_size = max(24, int(round(HOOK["size"] * sy)))
    strong = {_key(h) for h in (highlight_words or []) if _key(h)}
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{st['font']},{base_size},&H00{st['base']},&H00{st['base']},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,{st['outline']},0,5,0,0,0,1
Style: Hook,{HOOK['font']},{hook_size},&H00{HOOK['ink']},&H00{HOOK['ink']},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    hook_text = _clean(hook or "")
    if hook_enabled and hook_text:
        ev.extend(_hook_events(hook_text, dur, hook_y_s, width, height))
    f = _Font.get(st["font"])
    inside = []
    for w in (words_rel or []):
        nw = _norm(w)
        nw["text"] = _clean(nw["text"])
        if nw["text"] and nw["start"] < dur and nw["end"] > 0:
            nw["start"] = max(0.0, nw["start"])
            nw["end"] = min(dur, max(nw["start"], nw["end"]))
            inside.append(nw)
    inside.sort(key=lambda w: w["start"])
    for i, w in enumerate(inside[:-1]):  # word ends clamp to the next word's start
        w["end"] = max(w["start"], min(w["end"], inside[i + 1]["start"]))
    groups = group_words(inside, st["per"])
    for gi, g in enumerate(groups):
        g_start = max(0.0, g[0]["start"])
        nxt_start = groups[gi + 1][0]["start"] if gi + 1 < len(groups) else dur
        # Breath rule (stream-clips subs.py): a phrase stays on screen through
        # short breaths (< 1 s gap) so captions never flicker; longer pauses
        # let it drop 0.35 s after the last word.
        if nxt_start - g[-1]["end"] < 1.0:
            g_end = min(nxt_start, dur)
        else:
            g_end = min(g[-1]["end"] + 0.35, nxt_start, dur)
        if g_end - g_start < 0.02:
            continue
        texts = []
        for x in g:
            t = re.sub(r"[.,;:]+$", "", x["text"])
            texts.append(t.upper() if st["upper"] else t)
        # Scripts the caption font cannot draw: try Roman transliteration first
        # (Hinglish etc.), else draw the line whole and centred (PeakCut way).
        if not all(roman.drawable(t) for t in texts):
            try:
                conv = [roman.to_roman(t) if not roman.drawable(t) else t for t in texts]
            except Exception:  # noqa: BLE001
                conv = texts
            if all(roman.drawable(t) for t in conv):
                texts = conv
            else:
                line = " ".join(texts)
                fs = max(28, min(int(base_size * 0.7), int(1500 * (width / 1080.0) / max(1, len(line)) * 1.6)))
                ev.append(f"Dialogue: 1,{_ts(g_start)},{_ts(g_end)},Cap,,60,60,0,,"
                          f"{{\\an5\\pos({width // 2},{cap_y_s:.0f})\\q0\\fs{fs}}}{line}")
                continue
        if anim == "full_line":
            line = " ".join(texts)
            size = base_size
            if f.width(line, size) > cap_max_w:
                size = max(20, int(size * cap_max_w / max(1.0, f.width(line, size))))
            cy = cap_y_s - f.cap_shift(size)
            ev.append(f"Dialogue: 1,{_ts(g_start)},{_ts(g_end)},Cap,,0,0,0,,"
                      f"{{\\an5\\pos({width // 2},{cy:.0f})\\fs{size}\\c&H{st['base']}&}}{line}")
            continue
        size = base_size
        gap = f.width("n n", size) - 2 * f.width("n", size) + (2 * st["outline"] + 6) * size / max(1, st["size"])
        total = sum(f.width(t, size) for t in texts) + gap * (len(texts) - 1)
        if total > cap_max_w:  # a long phrase: shrink it to fit, never run off the frame
            size = max(20, int(size * cap_max_w / total))
            gap = f.width("n n", size) - 2 * f.width("n", size) + (2 * st["outline"] + 6) * size / max(1, st["size"])
            total = sum(f.width(t, size) for t in texts) + gap * (len(texts) - 1)
        cy = cap_y_s - f.cap_shift(size)
        x = (width - total) / 2
        for i, (w, t) in enumerate(zip(g, texts)):
            wd = f.width(t, size)
            p = max(104, min(st["pop"], 100 + int(2200 / max(wd, 1))))  # long words grow less
            if anim == "one_by_one":
                pos = f"\\an5\\pos({width // 2},{cy:.0f})\\fs{size}"
            else:
                pos = f"\\an5\\pos({x + wd / 2:.0f},{cy:.0f})\\fs{size}"
            x += wd + gap
            a = max(g_start, w["start"])
            b = (g[i + 1]["start"]) if i + 1 < len(g) else g_end
            b = max(a, min(b, g_end))
            base = st["strong"] if _key(w["text"]) in strong else st["base"]
            pop = f"\\fscx94\\fscy94\\t(0,90,\\fscx{p}\\fscy{p})\\t(90,200,\\fscx{p - 5}\\fscy{p - 5})"
            if anim == "highlight":
                if a - g_start >= 0.02:  # waiting to be said
                    ev.append(f"Dialogue: 1,{_ts(g_start)},{_ts(a)},Cap,,0,0,0,,{{{pos}\\c&H{base}&}}{t}")
                if b - a >= 0.02:  # being said: colour and a quick pop, in front
                    ev.append(f"Dialogue: 2,{_ts(a)},{_ts(b)},Cap,,0,0,0,,{{{pos}\\c&H{st['active']}&{pop}}}{t}")
                if g_end - b >= 0.02:  # said
                    ev.append(f"Dialogue: 1,{_ts(b)},{_ts(g_end)},Cap,,0,0,0,,"
                              f"{{{pos}\\c&H{base}&\\fscx{p - 5}\\fscy{p - 5}\\t(0,90,\\fscx100\\fscy100)}}{t}")
            elif anim == "pop_in":
                if b - a >= 0.02:  # appears as spoken, with a pop
                    ev.append(f"Dialogue: 2,{_ts(a)},{_ts(b)},Cap,,0,0,0,,{{{pos}\\c&H{st['active']}&{pop}}}{t}")
                if g_end - b >= 0.02:  # stays on screen, settled
                    ev.append(f"Dialogue: 1,{_ts(b)},{_ts(g_end)},Cap,,0,0,0,,"
                              f"{{{pos}\\c&H{base}&\\fscx{p - 5}\\fscy{p - 5}\\t(0,90,\\fscx100\\fscy100)}}{t}")
            elif anim == "build_up":
                if b - a >= 0.02:  # appears as spoken, no scale pop
                    ev.append(f"Dialogue: 2,{_ts(a)},{_ts(b)},Cap,,0,0,0,,{{{pos}\\c&H{st['active']}&}}{t}")
                if g_end - b >= 0.02:  # accumulates until the group ends
                    ev.append(f"Dialogue: 1,{_ts(b)},{_ts(g_end)},Cap,,0,0,0,,{{{pos}\\c&H{base}&}}{t}")
            elif anim == "one_by_one":
                if b - a >= 0.02:  # only the spoken word, centred
                    ev.append(f"Dialogue: 2,{_ts(a)},{_ts(b)},Cap,,0,0,0,,{{{pos}\\c&H{st['active']}&{pop}}}{t}")
    return head + "\n".join(ev) + "\n"


def build_srt(words_rel):
    """SubRip sidecar: grouped cues (4 words), comma timestamps."""
    out = []
    for i, g in enumerate(group_words(words_rel or [], 4)):
        if not g:
            continue
        line = " ".join(_clean(w["text"]) for w in g)
        out.append(f"{i + 1}\n{_stamp(g[0]['start'], True)} --> {_stamp(g[-1]['end'], True)}\n{line}\n")
    return "\n".join(out)


def build_vtt(words_rel):
    """WebVTT sidecar: grouped cues (4 words), dot timestamps, WEBVTT header."""
    out = ["WEBVTT\n"]
    for g in group_words(words_rel or [], 4):
        if not g:
            continue
        line = " ".join(_clean(w["text"]) for w in g)
        out.append(f"{_stamp(g[0]['start'], False)} --> {_stamp(g[-1]['end'], False)}\n{line}\n")
    return "\n".join(out)


def styles_catalog():
    """Caption styles for the UI."""
    return [{"id": k, "label": v["label"], "font": v["font"], "base": v["base"], "active": v["active"]}
            for k, v in STYLES.items()]
