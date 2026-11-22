"""Clip sidecars and publishing helpers: metadata JSON, ZIP bundles, uploads."""
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

_CLIP_EXTS = ("mp4", "jpg", "ass", "srt", "vtt", "json")


def write_metadata(clip, out_dir, name):
    """Write <name>.json beside a rendered clip. Returns the Path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    clip = clip or {}
    meta = {
        "title": clip.get("title") or "",
        "description": clip.get("description") or "",
        "caption": clip.get("caption") or clip.get("description") or "",
        "hashtags": clip.get("hashtags") or [],
        "hook": clip.get("hook") or "",
        "start": clip.get("start"),
        "end": clip.get("end"),
        "score": clip.get("score"),
        "viral_score": clip.get("viral_score", clip.get("score")),
        "kind": clip.get("kind") or "",
        "reason": clip.get("reason") or "",
        "why": clip.get("why") or "",
        "game_event": clip.get("game_event") or "",
        "look": clip.get("look") or "",
        "highlight_words": clip.get("highlight_words") or [],
        "generated_by": "ClipForge",
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }
    path = out_dir / f"{name}.json"
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _clock(seconds):
    s = max(0, int(seconds or 0))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


def posting_sheet(job, out_dir):
    """POSTING.md + clips.json for a finished job (stream-clips pipeline):
    per clip, the caption to paste, hashtags, why it should work, score and
    where it came from in the source. Returns the POSTING.md Path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    source = job.get("source") or {}
    clips = [c for c in (job.get("clips") or []) if c.get("status") == "done"]
    lines = [f"# {job.get('uploader') or 'Clips'}: {job.get('title') or source.get('name') or ''}", ""]
    if source.get("url"):
        lines.append(f"Source: {source['url']}  ")
    lines += [f"Covered: {_clock(0)} to {_clock(job.get('duration'))}  ",
              f"{len(clips)} clip(s) by ClipForge", "",
              "Watch every clip before posting. Only clip creators who allow it "
              "(many run paid clipping programs).", ""]
    records = []
    for c in clips:
        tags = c.get("hashtags") or []
        tag_line = " ".join(str(t) for t in tags) if isinstance(tags, list) else str(tags)
        caption = c.get("caption") or c.get("description") or ""
        secs = float(c.get("end") or 0) - float(c.get("start") or 0)
        lines += [f"## {c.get('file') or c.get('name') or ''}", "",
                  f"**{c.get('title') or ''}**  ",
                  f"{c.get('kind') or ''} · {secs:.0f}s · viral score {c.get('viral_score', c.get('score'))} "
                  f"· from {_clock(c.get('start'))} · look: {c.get('look') or c.get('layout') or ''}"
                  + (f" · game event: {c['game_event']}" if c.get("game_event") else ""), "",
                  "Caption to paste:", "", "```", caption + "\n" + tag_line, "```", "",
                  f"_Why it should work: {c.get('why') or c.get('reason') or ''}_", "", "---", ""]
        records.append({k: c.get(k) for k in
                        ("file", "name", "title", "hook", "caption", "description", "hashtags",
                         "kind", "score", "viral_score", "why", "reason", "game_event", "look",
                         "start", "end", "check")})
    md = out_dir / "POSTING.md"
    md.write_text("\n".join(lines), encoding="utf-8")
    (out_dir / "clips.json").write_text(json.dumps(
        {"source": {"title": job.get("title"), "uploader": job.get("uploader"),
                    "url": source.get("url"), "duration": job.get("duration")},
         "clips": records}, ensure_ascii=False, indent=2), encoding="utf-8")
    return md


def export_zip(job_dir, clip_names, out_path):
    """Bundle clips into one ZIP (stored, no recompression) + clips.txt summary.

    clip_names are base names (e.g. "clip01"); each clip's mp4/jpg/ass/srt/
    vtt/json that exists is included. clips.txt lists title + description +
    hashtags per clip (read from each clip's JSON sidecar when present),
    blank-line separated. Returns the Path of the ZIP.
    """
    job_dir = Path(job_dir)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    summaries = []
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_STORED) as zf:
        for extra in ("POSTING.md", "clips.json"):
            f = job_dir / extra
            if f.is_file():
                zf.write(f, arcname=extra)
        for name in clip_names or []:
            name = str(name)
            for ext in _CLIP_EXTS:
                f = job_dir / f"{name}.{ext}"
                if f.is_file():
                    zf.write(f, arcname=f"{name}.{ext}")
            check = job_dir / f"{name}_check.jpg"
            if check.is_file():
                zf.write(check, arcname=f"{name}_check.jpg")
            title, description, hashtags = name, "", ""
            meta_file = job_dir / f"{name}.json"
            if meta_file.is_file():
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    title = str(meta.get("title") or name)
                    description = str(meta.get("description") or "")
                    tags = meta.get("hashtags") or []
                    hashtags = " ".join(str(t) for t in tags) if isinstance(tags, list) else str(tags)
                except (ValueError, OSError):
                    pass
            block = title
            if description:
                block += f"\n{description}"
            if hashtags:
                block += f"\n{hashtags}"
            summaries.append(block)
        zf.writestr("clips.txt", "\n\n".join(summaries) + ("\n" if summaries else ""))
    return out_path


def youtube_upload(clip_path, metadata, client_secrets=None, token_file=None):
    """Upload a clip to YouTube. STUB — deliberately not implemented.

    ClipForge stores no credentials and ships no Google API client. To
    enable direct uploads, the user must:
      1. Create a Google Cloud project and an OAuth 2.0 *Desktop* client
         (APIs & Services -> Credentials) with the YouTube Data API v3
         enabled, and download its client_secrets.json.
      2. Run the one-time consent flow (google-auth-oauthlib's
         InstalledAppFlow) to mint a token.json for the channel.
      3. Implement the upload here with google-api-python-client:
         youtube.videos().insert with a resumable MediaFileUpload,
         snippet categoryId 20 (Gaming), and "#Shorts" in the title or
         description so vertical clips are treated as Shorts.
    Until then, clips are uploaded manually from the exported files.
    """
    raise NotImplementedError(
        "YouTube direct upload is a stub in ClipForge: ClipForge stores no credentials. "
        "To enable it, create a Google Cloud OAuth Desktop client, download client_secrets.json, "
        "run the one-time consent flow to mint token.json, then implement this with "
        "google-api-python-client (resumable upload, categoryId 20, #Shorts).")
