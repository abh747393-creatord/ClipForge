"""Subtitle import: SRT / WebVTT / ASS text -> word-timed words.

Imported subtitles give ClipForge captions without running Whisper: a
creator who already has a subtitle file (or a downloaded .srt) can feed
it straight into the caption pipeline. Words use the shared shape
{"start", "end", "text"} in seconds (same as transcribe/captions_yt).

Cues carry one time span for several words, so each cue's span is spread
over its words in proportion to word length (longer words take longer
to say): word duration ~ len(word) + 1.
"""
import html
import re

_FORMATS = {"srt": "srt", "subrip": "srt", "vtt": "vtt", "webvtt": "vtt",
            "ass": "ass", "ssa": "ass"}

_TAG_RX = re.compile(r"<[^>]*>")
_ASS_OVERRIDE_RX = re.compile(r"\{[^}]*\}")
_WS_RX = re.compile(r"\s+")


def _parse_time(value):
    """'HH:MM:SS,mmm' / 'MM:SS.mmm' / ASS 'H:MM:SS.cc' -> seconds float, or None."""
    try:
        parts = str(value).strip().replace(",", ".").split(":")
        if len(parts) == 3:
            hours, minutes, seconds = parts
        elif len(parts) == 2:
            hours, minutes, seconds = "0", parts[0], parts[1]
        else:
            return None
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (TypeError, ValueError):
        return None


def _clean_text(text):
    """Strip markup tags, unescape entities, collapse whitespace."""
    text = _TAG_RX.sub("", text or "")
    text = html.unescape(text)
    return _WS_RX.sub(" ", text).strip()


def _detect(text):
    """Subtitle format from content: 'ass' | 'vtt' | 'srt' | None."""
    head = text.lstrip("\ufeff \t\r\n")
    if head.startswith("WEBVTT"):
        return "vtt"
    if "[Events]" in text or "Dialogue:" in text:
        return "ass"
    if "-->" in text:
        return "srt"
    return None


def _parse_arrow_cues(text):
    """Cues from SRT/WebVTT text (both use 'start --> end' timing lines).

    Cue numbers/identifiers before a timing line are skipped, VTT cue
    settings after the end time are ignored, and a cue body runs until
    the next blank line (or the next cue's number, for files without
    blank-line separators)."""
    lines = text.splitlines()
    cues = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if "-->" not in line:
            i += 1
            continue
        left, _, right = line.partition("-->")
        left = left.strip()
        start = _parse_time(left.split()[-1] if left else "")
        right = right.strip()
        end = _parse_time(right.split()[0] if right else "")
        i += 1
        body = []
        while i < n:
            cur = lines[i]
            if not cur.strip():
                i += 1
                break
            if cur.strip().isdigit() and i + 1 < n and "-->" in lines[i + 1]:
                break  # next cue's number (no blank-line separator)
            body.append(cur)
            i += 1
        cue_text = _clean_text(" ".join(body))
        if start is not None and end is not None and end > start and cue_text:
            cues.append({"start": round(start, 3), "end": round(end, 3), "text": cue_text})
    return cues


def _parse_ass_cues(text):
    """Cues from ASS/SSA text (Dialogue: lines; field order from Format:).

    Override tags ({\\pos(...)}, {\\b1}...) are stripped and \\N / \\n /
    \\h become spaces. The Text field is last, so commas inside the
    dialogue survive."""
    fields = ["layer", "start", "end", "style", "name",
              "marginl", "marginr", "marginv", "effect", "text"]
    cues = []
    for raw in text.splitlines():
        line = raw.strip()
        lowered = line.lower()
        if lowered.startswith("format:"):
            got = [f.strip().lower() for f in line.split(":", 1)[1].split(",")]
            if "start" in got and "end" in got and "text" in got:
                fields = got
            continue
        if not lowered.startswith("dialogue:"):
            continue
        rest = line.split(":", 1)[1].lstrip()
        parts = rest.split(",", len(fields) - 1)
        if len(parts) < len(fields):
            continue
        row = dict(zip(fields, parts))
        start = _parse_time(row.get("start", ""))
        end = _parse_time(row.get("end", ""))
        body = _ASS_OVERRIDE_RX.sub("", row.get("text", ""))
        body = body.replace("\\N", " ").replace("\\n", " ").replace("\\h", " ")
        cue_text = _clean_text(body)
        if start is not None and end is not None and end > start and cue_text:
            cues.append({"start": round(start, 3), "end": round(end, 3), "text": cue_text})
    return cues


def parse_cues(text, fmt=None) -> list:
    """Subtitle text -> [{"start", "end", "text"}] cues sorted by start.

    fmt is 'srt' | 'vtt' | 'ass' (aliases ok); None auto-detects from the
    content. Unparseable input returns [] (words_from_text is the strict
    entry point)."""
    text = (text or "").lstrip("\ufeff")
    if fmt is None:
        fmt = _detect(text)
    else:
        fmt = _FORMATS.get(str(fmt).strip().lower().lstrip("."))
    if fmt == "ass":
        cues = _parse_ass_cues(text)
    elif fmt in ("srt", "vtt"):
        cues = _parse_arrow_cues(text)
    else:
        return []
    cues.sort(key=lambda c: c["start"])
    return cues


def words_from_text(text, fmt=None) -> list:
    """Subtitle text -> word list [{"start", "end", "text"}] sorted by start.

    fmt: 'srt' | 'vtt' | 'ass' or None to auto-detect. Each cue's span is
    spread over its words by letter count (duration ~ len(word) + 1).
    Raises ValueError when nothing parseable is found."""
    words = []
    for cue in parse_cues(text, fmt):
        parts = cue["text"].split()
        if not parts:
            continue
        span = cue["end"] - cue["start"]
        weights = [len(w) + 1 for w in parts]
        total = float(sum(weights)) or 1.0
        at = cue["start"]
        for j, word in enumerate(parts):
            nxt = cue["end"] if j == len(parts) - 1 else at + span * weights[j] / total
            words.append({"start": round(at, 3), "end": round(max(nxt, at + 0.01), 3),
                          "text": word})
            at = nxt
    if not words:
        raise ValueError("could not parse any subtitles from the given text")
    words.sort(key=lambda w: w["start"])
    return words
