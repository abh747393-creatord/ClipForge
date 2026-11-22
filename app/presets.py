"""Style presets: named bundles over ClipForge's existing knobs.

Inspired by Wanted's style presets (Heist, Retro 16-bit, Anime opening,
Movie trailer...). A preset sets layout / caption style / animation /
extras, and may add a `preset_filter` — an FFmpeg filter chain applied to
the laid-out video before captions (pixelize, saturation, letterbox...).
User-chosen options always win over preset values.
"""

PRESETS = {
    "clean_pro": {
        "label": "Clean Pro",
        "desc": "Smart auto framing, clean captions, no gimmicks",
        "options": {"layout": "auto", "style": "clean", "anim": "highlight"},
    },
    "heist": {
        "label": "Heist",
        "desc": "GTA neon captions + wanted stars",
        "options": {"layout": "auto", "style": "gta", "anim": "pop_in",
                    "gta_overlay": {"stars": "auto", "mission_card": True}},
    },
    "retro16": {
        "label": "Retro 16-bit",
        "desc": "Pixelated picture, bold arcade captions",
        "options": {"layout": "fill", "style": "bold", "anim": "one_by_one",
                    "preset_filter": "pixelize=10:10,eq=saturation=1.25:contrast=1.05"},
    },
    "anime": {
        "label": "Anime Opening",
        "desc": "Saturated colours, hype captions",
        "options": {"layout": "auto", "style": "hype", "anim": "pop_in",
                    "preset_filter": "eq=saturation=1.45:contrast=1.08,unsharp=5:5:0.6"},
    },
    "trailer": {
        "label": "Movie Trailer",
        "desc": "Letterbox bars, big hook, clean captions",
        "options": {"layout": "blur", "style": "clean", "anim": "full_line",
                    "preset_filter": "drawbox=x=0:y=0:w=iw:h=ih*0.09:color=black:t=fill,"
                                     "drawbox=x=0:y=ih*0.91:w=iw:h=ih*0.09:color=black:t=fill,"
                                     "eq=contrast=1.12:saturation=0.92"},
    },
    "news": {
        "label": "Breaking News",
        "desc": "News lower-third template with the clip title",
        "options": {"layout": "blur", "style": "clean", "anim": "highlight",
                    "template": "news"},
    },
    "viral": {
        "label": "Viral Bounce",
        "desc": "Bouncy word captions, title bar, kill-cam replay on game events",
        "options": {"layout": "auto", "style": "bounce", "anim": "highlight",
                    "title_bar": True, "killcam": True},
    },
}


def catalog():
    return [{"id": k, "label": v["label"], "desc": v["desc"]} for k, v in PRESETS.items()]


def apply(options: dict) -> dict:
    """Return options with the chosen preset's values filled in where the
    user did not set their own. `options['_preset_applied']` records the id.
    Unknown preset ids are ignored. Never raises."""
    options = dict(options or {})
    pid = options.get("preset")
    if pid not in PRESETS:
        return options
    for key, value in PRESETS[pid]["options"].items():
        if key == "gta_overlay":
            merged = dict(value)
            merged.update(options.get("gta_overlay") or {})
            options["gta_overlay"] = merged
        elif key not in options or options.get(key) is None:
            options[key] = value
    options["_preset_applied"] = pid
    return options
