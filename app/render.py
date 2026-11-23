"""Turns a time range into a finished clip: layout, captions, cover, sidecars.

Merged from PeakCut's studio/render.py render_clip/_render_in (temp work
dir, loudnorm, faststart, cover frame, embedded metadata) and
NeonClipping's app.py export() (layout graph + ASS burn, music bed,
watermark/template extras).
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import captions, config, facecam, layouts, media, publish, titlecard

RENDER_SECONDS = 600  # one clip's encode; far beyond normal, so a frozen encoder is stopped


def _w(word, *keys, default=None):
    for k in keys:
        if isinstance(word, dict) and word.get(k) is not None:
            return word[k]
    return default


def _rect_px(rect, src_w, src_h):
    """Normalised {x, y, w, h} (0..1) -> source pixels (even, clamped), or None."""
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


def _has_audio(src):
    """True/False from media.probe; unknown shapes default to True (ffmpeg decides)."""
    try:
        info = media.probe(src)
    except Exception as e:  # noqa: BLE001
        return "audio" not in str(e).lower()
    if isinstance(info, dict):
        if "has_audio" in info:
            return bool(info["has_audio"])
        if "audio" in info:
            return bool(info["audio"])
        streams = info.get("streams")
        if isinstance(streams, list):
            return any(isinstance(s, dict) and s.get("codec_type") == "audio" for s in streams)
    return True


def _opacity(value, default=0.7):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v > 1.0:  # accept percentages too
        v = v / 100.0
    return max(0.0, min(1.0, v))


def _wm_xy(position, width, height, text=False):
    """(x, y) filter expressions for a watermark corner (no commas: safe unquoted)."""
    m = max(8, round(20 * width / 1080))
    pos = str(position or "br").lower().replace("_", "-")
    if pos in ("tl", "top-left", "top_left"):
        return (str(m), str(m))
    if pos in ("tr", "top-right", "top_right"):
        return (f"{'w' if text else 'W'}-{'text_w' if text else 'w'}-{m}", str(m))
    if pos in ("bl", "bottom-left", "bottom_left"):
        return (str(m), f"{'h' if text else 'H'}-{'text_h' if text else 'h'}-{m}")
    return (f"{'w' if text else 'W'}-{'text_w' if text else 'w'}-{m}",
            f"{'h' if text else 'H'}-{'text_h' if text else 'h'}-{m}")


_ENCODER_CACHE = {"probed": False, "enc": None}


def _probe_encoder():
    """First hardware encoder that actually encodes a test frame, else None.
    Ported from stream-clips produce.video_encoder(). Cached per process."""
    if _ENCODER_CACHE["probed"]:
        return _ENCODER_CACHE["enc"]
    _ENCODER_CACHE["probed"] = True
    for enc in ("h264_nvenc", "h264_qsv", "h264_vaapi", "h264_videotoolbox", "h264_amf"):
        test = ["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                "color=c=black:s=256x256:d=0.2", "-c:v", enc, "-f", "null", "-"]
        try:
            if subprocess.run(test, capture_output=True, timeout=60,
                              creationflags=getattr(media, "NO_WINDOW", 0)).returncode == 0:
                _ENCODER_CACHE["enc"] = enc
                return enc
        except Exception:  # noqa: BLE001
            continue
    return None


def _video_args():
    """Video codec args. CLIPFORGE_ENCODER forces a codec; otherwise a
    working hardware encoder is probed once, else libx264."""
    enc = (os.environ.get("CLIPFORGE_ENCODER") or "").strip() or _probe_encoder()
    if not enc:
        return ["-c:v", "libx264", "-crf", "20", "-preset", "veryfast"]
    if "nvenc" in enc:
        return ["-c:v", enc, "-cq", "20"]
    if "qsv" in enc:
        return ["-c:v", enc, "-global_quality", "23"]
    if "videotoolbox" in enc or "amf" in enc or "vaapi" in enc:
        return ["-c:v", enc, "-b:v", "10M", "-maxrate", "14M", "-bufsize", "20M"]
    return ["-c:v", enc, "-crf", "20", "-preset", "veryfast"]


def _check_sheet(mp4, dur, dest):
    """Three frames side by side (20/50/80%) so a person — or an agent that
    can see images — can QC framing, hook and captions without playing the
    video. Ported from stream-clips produce.check_image()."""
    from PIL import Image
    shots = []
    for i, f in enumerate((0.2, 0.5, 0.8)):
        tmp = dest.with_name(dest.stem + f"_{i}.jpg")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-nostdin", "-ss", f"{dur * f:.2f}",
                        "-i", str(mp4), "-frames:v", "1", "-vf", "scale=360:-2", str(tmp)],
                       capture_output=True, creationflags=getattr(media, "NO_WINDOW", 0), timeout=120)
        with Image.open(tmp) as im:
            shots.append(im.convert("RGB"))
        tmp.unlink(missing_ok=True)
    sheet = Image.new("RGB", (sum(s.width for s in shots) + 20, shots[0].height), (20, 20, 20))
    x = 0
    for s in shots:
        sheet.paste(s, (x, 0))
        x += s.width + 10
    sheet.save(dest, quality=85)
    return dest


def _killcam(mp4, work, name, dur):
    """Append a slow-mo replay of the loudest moment (Wanted's kill-cam).
    Returns the new duration. Raises on failure (caller keeps the original)."""
    mp4 = Path(mp4)
    wav = work / "kc.wav"
    media.extract_audio(mp4, wav, sr=16000)
    levels = media.loudness_per_second(wav)
    peak = float(max(range(len(levels)), key=lambda i: levels[i])) if levels else dur / 2
    lo = max(0.0, peak - 1.25)
    hi = min(dur, peak + 1.25)
    if hi - lo < 0.5:
        return dur
    out = work / f"{name}_kc.mp4"
    graph = (
        "[0:v]trim=0:{d:.3f},setpts=PTS-STARTPTS[main];"
        "[0:a]atrim=0:{d:.3f},asetpts=PTS-STARTPTS[maina];"
        "[0:v]trim={lo:.3f}:{hi:.3f},setpts=2*(PTS-STARTPTS),"
        "drawtext=fontfile=f/ArchivoBlack-Regular.ttf:text='REPLAY':fontsize=54:fontcolor=white"
        ":borderw=3:bordercolor=black:x=36:y=36[rep];"
        "[0:a]atrim={lo:.3f}:{hi:.3f},asetpts=PTS-STARTPTS,atempo=0.5[repa];"
        "[main][maina][rep][repa]concat=n=2:v=1:a=1[v][a]"
    ).format(d=dur, lo=lo, hi=hi)
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(mp4), "-filter_complex", graph,
           "-map", "[v]", "-map", "[a]", *_video_args(), "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart", str(out)]
    proc = subprocess.run(cmd, cwd=work, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=getattr(media, "NO_WINDOW", 0),
                          timeout=RENDER_SECONDS)
    if proc.returncode != 0 or not out.is_file():
        raise RuntimeError(f"kill-cam failed: {(proc.stderr or '')[-600:]}")
    out.replace(mp4)
    return dur + (hi - lo) * 2.0


def probe_output(path):
    """{"duration", "width", "height"} of a rendered file, via media.probe."""
    info = media.probe(path) or {}
    return {"duration": float(info.get("duration") or 0.0),
            "width": int(info.get("width") or 0),
            "height": int(info.get("height") or 0)}


def render_clip(src, out_dir, name, clip, words_abs, src_w, src_h, options):
    """Render one clip. Writes <name>.mp4/.jpg/.ass/.srt/.vtt/.json to out_dir.

    clip: {start, end, title, hook, description, hashtags, highlight_words,
    ...} (may also carry facecam/game rects). words_abs are source-timed
    Word dicts. options is the shared options dict. Returns file names plus
    the final layout, duration and output size.
    """
    src, out_dir = Path(src), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    clip, options = clip or {}, options or {}
    start, end = float(clip["start"]), float(clip["end"])
    dur = end - start
    if dur <= 0:
        raise ValueError("Clip end must be after its start.")
    src_w, src_h = int(src_w), int(src_h)

    # 1. Words relative to the clip start, ends clamped to the clip.
    words_rel = []
    for w in words_abs or []:
        ws = float(_w(w, "start", "s", default=0.0))
        we = float(_w(w, "end", "e", default=ws))
        text = str(_w(w, "text", "w", default="") or "")
        if start <= ws < end and text.strip():
            rel_s = ws - start
            rel_e = max(rel_s, min(we, end) - start)
            words_rel.append({"start": rel_s, "end": rel_e, "text": text})
    words_rel.sort(key=lambda w: w["start"])

    # 2. Output size.
    width, height, fps = layouts.output_settings(options)
    layout = str(options.get("layout") or "blur")
    top = float(options.get("top", 0.32))
    caption_y = int(options.get("caption_y", 1450))

    # 3. Camera / game rects (clip first, then options; detect once if needed).
    cam = clip.get("facecam") or options.get("facecam")
    game = clip.get("game") or options.get("game")
    follow_spec = None
    look_why = ""
    if layout == "auto":
        # Smart look per clip (stream-clips frame.plan): small live face ->
        # stack, one big face -> follow, several faces / none -> blur.
        look = facecam.auto_look(src, start, dur, src_w, src_h, forced_cam=cam)
        layout = look["layout"]
        cam = look["cam"] or cam
        look_why = look.get("why") or ""
        if layout == "follow" and look.get("track") is not None:
            n_frames = len(look["frames"]) if look.get("frames") is not None else 0
            follow_spec = facecam.follow_keys(look["track"], n_frames, look["scale"],
                                              src_w, src_h, width, height)
    if layout == "follow" and follow_spec is None:
        tracks, frames, scale = facecam.face_tracks(src, start, dur, src_w, src_h)
        live = [t for t in tracks if t["alive"] and t["presence"] >= 0.3]
        if live:
            follow_spec = facecam.follow_keys(live[0], len(frames), scale, src_w, src_h, width, height)
        else:
            layout = "fill"  # no face to follow
    if layouts.needs_cam(layout) and cam is None:
        cam = facecam.detect(src, end, src_w, src_h)
        clip["facecam"] = cam
    if layouts.needs_cam(layout) and cam is None:
        layout = "fill"  # pip_*/circle/stack/bottom/side without a camera -> fill
    if layout == "game" and game is None:
        layout = "fill"
    cam_px = _rect_px(cam, src_w, src_h)
    game_px = _rect_px(game, src_w, src_h)

    # 4. Captions.
    pos = layouts.text_positions(layout, top, caption_y, width, height)
    use_captions = bool(options.get("captions", True))
    hook_enabled = bool(options.get("hook_enabled", True))
    hook_text = str(clip.get("hook") or "")
    # Title bar (Pillow PNG, colour emoji) replaces the ASS hook box.
    use_title_bar = bool(options.get("title_bar")) and hook_enabled and bool(hook_text.strip())
    eff_words = words_rel if use_captions else []
    ass = captions.build_ass(
        eff_words, dur,
        hook_text if hook_enabled else "",
        str(options.get("style") or "bounce"),
        str(options.get("anim") or "highlight"),
        clip.get("highlight_words") or [],
        pos["hook_y"], pos["caption_y"], width, height,
        hook_enabled=hook_enabled and not use_title_bar)

    # Work in a short temp dir with relative names (long paths break the
    # ffmpeg ass filter on Windows; a drive colon would break its syntax).
    work = Path(tempfile.mkdtemp(prefix="cf"))
    try:
        (work / f"{name}.ass").write_text(ass, encoding="utf-8")
        (work / "f").mkdir()
        for fnt in Path(config.FONTS_DIR).glob("*.ttf"):
            shutil.copyfile(fnt, work / "f" / fnt.name)

        # Inputs: 0 = source; then silent track / music / watermark image.
        inputs = ["-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", str(src)]
        next_idx = 1
        has_audio = _has_audio(src)
        voice_idx = 0
        if not has_audio:
            inputs += ["-f", "lavfi", "-t", f"{dur:.2f}", "-i",
                       "anullsrc=channel_layout=stereo:sample_rate=48000"]
            voice_idx = next_idx
            next_idx += 1
        music = options.get("music") or {}
        music_path = music.get("path") if isinstance(music, dict) else None
        music_idx = None
        if music_path and Path(str(music_path)).is_file():
            inputs += ["-stream_loop", "-1", "-i", str(music_path)]
            music_idx = next_idx
            next_idx += 1
        wm = options.get("watermark") or {}
        wm_image = wm.get("image") if isinstance(wm, dict) else None
        wm_idx = None
        if wm_image and Path(str(wm_image)).is_file():
            inputs += ["-i", str(wm_image)]
            wm_idx = next_idx
            next_idx += 1

        # PNG overlays: title bar, wanted stars, mission card.
        png_inputs = {}  # role -> (input_index, w, h)
        if use_title_bar:
            try:
                _p, bw, bh = titlecard.render(hook_text, work / "titlebar.png", width=width,
                                              size=max(28, round(56 * height / 1920)))
                inputs += ["-i", str(work / "titlebar.png")]
                png_inputs["title"] = (next_idx, bw, bh)
                next_idx += 1
            except Exception:  # noqa: BLE001 - the bar is decoration; never fail a render for it
                pass
        gta = options.get("gta_overlay") or {}
        if isinstance(gta, dict) and gta.get("stars"):
            try:
                stars = gta["stars"]
                if stars == "auto":  # 1-5 stars from the clip's score
                    stars = max(1, min(5, round(float(clip.get("score") or 50) / 20)))
                _p, bw, bh = titlecard.stars_png(work / "stars.png", int(stars),
                                                 star_px=max(28, round(54 * height / 1920)))
                inputs += ["-i", str(work / "stars.png")]
                png_inputs["stars"] = (next_idx, bw, bh)
                next_idx += 1
            except Exception:  # noqa: BLE001
                pass
        if isinstance(gta, dict) and gta.get("mission_card"):
            try:
                _p, bw, bh = titlecard.mission_card_png(work / "mission.png", width=width,
                                                        size=max(32, round(64 * height / 1920)))
                inputs += ["-i", str(work / "mission.png")]
                png_inputs["mission"] = (next_idx, bw, bh)
                next_idx += 1
            except Exception:  # noqa: BLE001
                pass

        # 5. Filtergraph: layout -> preset filter -> template/watermark ->
        #    PNG overlays -> campaign banner -> ASS burn (+ audio mix).
        parts = [layouts.render_graph(layout, (cam_px, game_px), top, width, height,
                                      cam_fx=str(options.get("cam_fx") or "unsharp=5:5:0.8:5:5:0.0"),
                                      follow=follow_spec, src_w=src_w, src_h=src_h)]
        cur = "base"
        preset_filter = str(options.get("preset_filter") or "").strip()
        if preset_filter:
            parts.append(f"[{cur}]{preset_filter}[pfx]")
            cur = "pfx"
        if str(options.get("template") or "") == "news":
            bar = max(48, round(140 * height / 1920))
            title = " ".join(str(clip.get("title") or clip.get("hook") or "").split())
            (work / "title.txt").write_text(title, encoding="utf-8")
            fs = max(20, round(56 * height / 1920))
            tx = max(16, round(40 * width / 1080))
            ty = height - bar + max(8, round(bar * 0.3))
            parts.append(
                f"[{cur}]drawbox=x=0:y={height - bar}:w={width}:h={bar}:color=black@0.75:t=fill,"
                f"drawtext=fontfile=f/ArchivoBlack-Regular.ttf:textfile=title.txt:fontsize={fs}"
                f":fontcolor=white:x={tx}:y={ty}[tpl]")
            cur = "tpl"
        wm_text = str(wm.get("text") or "").strip() if isinstance(wm, dict) else ""
        if wm_text:
            (work / "wm.txt").write_text(" ".join(wm_text.split()), encoding="utf-8")
            op = _opacity(wm.get("opacity"), 0.7)
            wx, wy = _wm_xy(wm.get("position"), width, height, text=True)
            wfs = max(16, round(40 * height / 1920))
            parts.append(
                f"[{cur}]drawtext=fontfile=f/Poppins-ExtraBold.ttf:textfile=wm.txt:fontsize={wfs}"
                f":fontcolor=white@{op:.2f}:borderw=2:bordercolor=black@{op:.2f}:x={wx}:y={wy}[wmt]")
            cur = "wmt"
        if wm_idx is not None:
            op = _opacity(wm.get("opacity"), 0.7)
            wm_w = max(32, round(width * 0.18) // 2 * 2)
            wx, wy = _wm_xy(wm.get("position"), width, height, text=False)
            parts.append(f"[{wm_idx}:v]scale={wm_w}:-2,format=rgba,colorchannelmixer=aa={op:.2f}[wmimg]")
            parts.append(f"[{cur}][wmimg]overlay={wx}:{wy}[wmi]")
            cur = "wmi"
        if "title" in png_inputs:
            idx, bw, bh = png_inputs["title"]
            ty = max(16, int(pos["hook_y"] * height / 1920) - bh // 2)
            parts.append(f"[{cur}][{idx}:v]overlay=(W-w)/2:{ty}[tbar]")
            cur = "tbar"
        if "stars" in png_inputs:
            idx, bw, bh = png_inputs["stars"]
            margin = max(12, round(24 * width / 1080))
            parts.append(f"[{cur}][{idx}:v]overlay=W-w-{margin}:{margin}[strs]")
            cur = "strs"
        if "mission" in png_inputs:
            idx, bw, bh = png_inputs["mission"]
            my = int(height * 0.16)
            if "title" in png_inputs:  # sit below the title bar, never on top of it
                _ti, _tw, th = png_inputs["title"]
                my = max(my, int(pos["hook_y"] * height / 1920) + th // 2 + 24)
            parts.append(f"[{cur}][{idx}:v]overlay=(W-w)/2:{my}:enable='lt(t,2.5)'[msn]")
            cur = "msn"
        campaign = options.get("campaign") or {}
        if isinstance(campaign, dict) and str(campaign.get("handle") or "").strip():
            handle = str(campaign["handle"]).strip()
            if not handle.startswith("@"):
                handle = "@" + handle
            tag = str(campaign.get("tag") or "#ad").strip()
            (work / "campaign.txt").write_text(" ".join(f"{handle} {tag}".split()), encoding="utf-8")
            cfs = max(20, round(44 * height / 1920))
            parts.append(
                f"[{cur}]drawtext=fontfile=f/Poppins-ExtraBold.ttf:textfile=campaign.txt:fontsize={cfs}"
                f":fontcolor=white:borderw=2:bordercolor=black:x=(w-text_w)/2:y={max(12, round(24 * height / 1920))}"
                f":box=1:boxcolor=black@0.45:boxborderw=10[cmp]")
            cur = "cmp"
        parts.append(f"[{cur}]ass={name}.ass:fontsdir=f[v]")

        # 6. Audio: loudnorm; optional music bed mixed under the voice.
        if music_idx is not None:
            try:
                vol = float(music.get("volume", 0.15))
            except (TypeError, ValueError):
                vol = 0.15
            parts.append(f"[{voice_idx}:a]loudnorm=I=-14:TP=-1.5:LRA=11[voice]")
            parts.append(f"[{music_idx}:a]volume={vol:.3f}[mus]")
            parts.append("[voice][mus]amix=inputs=2:duration=first:dropout_transition=0[aout]")
            audio_map = ["-map", "[aout]"]
            audio_filter = []
        else:
            audio_map = ["-map", f"{voice_idx}:a:0"]
            audio_filter = ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]

        # 7. Encode.
        cmd = ["ffmpeg", "-y", "-v", "error", *inputs,
               "-filter_complex", ";".join(parts),
               "-map", "[v]", *audio_map, *audio_filter,
               *_video_args(), "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
               "-movflags", "+faststart"]
        if fps:
            cmd += ["-r", str(fps), "-fps_mode", "cfr"]
        title = str(clip.get("title") or "")
        desc = str(clip.get("description") or "")
        if title:
            cmd += ["-metadata", f"title={title}"]
        if desc:
            cmd += ["-metadata", f"description={desc}"]
        cmd += [f"{name}.mp4"]
        proc = subprocess.run(cmd, cwd=work, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              creationflags=getattr(media, "NO_WINDOW", 0),
                              timeout=RENDER_SECONDS)
        if proc.returncode != 0:
            raise RuntimeError(f"Render failed: {(proc.stderr or '')[-1500:]}")

        # 7b. Kill-cam replay (optional post-pass on the finished clip).
        if options.get("killcam"):
            try:
                dur = _killcam(work / f"{name}.mp4", work, name, dur)
            except Exception:  # noqa: BLE001 - replay is a bonus; keep the plain clip
                pass

        # 8. Cover + sidecars.
        cover_t = min(1.2, dur / 2)
        cover_path = work / f"{name}.jpg"
        try:
            media.frame_at(work / f"{name}.mp4", cover_t, cover_path, width)
            if not cover_path.is_file():
                raise RuntimeError("frame_at produced no cover")
        except Exception:  # noqa: BLE001 - fall back to a direct ffmpeg grab
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{cover_t:.2f}",
                            "-i", f"{name}.mp4", "-frames:v", "1", "-q:v", "3", f"{name}.jpg"],
                           cwd=work, capture_output=True,
                           creationflags=getattr(media, "NO_WINDOW", 0), timeout=120)
        (work / f"{name}.srt").write_text(captions.build_srt(eff_words), encoding="utf-8")
        (work / f"{name}.vtt").write_text(captions.build_vtt(eff_words), encoding="utf-8")
        check_name = None
        try:
            _check_sheet(work / f"{name}.mp4", dur, work / f"{name}_check.jpg")
            check_name = f"{name}_check.jpg"
        except Exception:  # noqa: BLE001 - QC sheet is a bonus
            check_name = None

        # 9. Move outputs into place; write metadata JSON.
        for ext in ("mp4", "jpg", "ass", "srt", "vtt"):
            src_file = work / f"{name}.{ext}"
            if src_file.is_file():
                shutil.move(str(src_file), str(out_dir / f"{name}.{ext}"))
        if check_name and (work / check_name).is_file():
            shutil.move(str(work / check_name), str(out_dir / check_name))
        clip["look"] = look_why or layout
        publish.write_metadata(clip, out_dir, name)
        return {"file": f"{name}.mp4", "cover": f"{name}.jpg", "ass": f"{name}.ass",
                "srt": f"{name}.srt", "vtt": f"{name}.vtt", "meta": f"{name}.json",
                "check": check_name, "look": clip["look"],
                "layout": layout, "duration": float(dur), "width": int(width), "height": int(height)}
    finally:
        shutil.rmtree(work, ignore_errors=True)
