# ClipForge

One professional tool that turns streams and videos into short vertical
clips — automatically. Paste a YouTube / Twitch / Kick link (or upload a
file), and ClipForge transcribes it, watches the audio **and the game** for
moments, picks the best ones, and renders captioned 1080×1920 clips with
hooks, covers, subtitle sidecars and a ready-to-paste posting sheet.

ClipForge combines the best of three sources:

- **NeonClipping** — signals-first moment finding (works with no AI key),
  facecam + gameplay region selection, grounded hooks.
- **PeakCut** — the clean studio UI, Hinglish captions, live mode, loudnorm.
- **stream-clips** (skill) — moment taxonomy, signal-marked AI prompts,
  pause-aware snapping, auto framing, check sheets, POSTING.md, discovery.
- Plus ideas from **Wanted** (gta6clipmaker.com): game-event detection,
  viral score, kill-cam replay, GTA overlays, style presets, campaign mode.

## Run it

```bash
cd ~/workspace/clipforge
./run.sh            # creates .venv + installs deps on first run
```

Then open **http://127.0.0.1:8780**

Manual start (same thing):

```bash
CLIPFORGE_PORT=8780 .venv/bin/python -m app
```

Requirements: Python 3.10+, ffmpeg/ffprobe on PATH. Everything else is in
`requirements.txt`. The Whisper model downloads on first use (~150 MB for
`base`). No accounts, no API keys required — an LLM is optional (Settings
tab, any OpenAI-compatible endpoint, e.g. a local Ollama).

## How it works

1. **Ingest** — yt-dlp (YouTube/Twitch/Kick) or MP4 upload. Long URLs use an
   audio-first fast path: audio only → transcript → only the chosen
   sections are downloaded as video and aligned by audio correlation.
2. **Transcribe** — borrowed YouTube captions when available, else local
   faster-whisper (word timings). Hinglish mode transliterates Hindi speech
   to Roman script. Optional two-model mode: fast model scans, accurate
   model re-transcribes each clip for captions.
3. **Scan** — per-second signals: optical-flow motion, scene cuts,
   loudness jump, shout band, volume spikes vs the local baseline,
   excitement lexicon, YouTube chat-replay speed, and **game events**
   (WASTED/BUSTED screens, explosions, crashes, chases — detected visually
   with OpenCV; OCR confirms the word when tesseract is installed).
4. **Pick** — signal candidates snapped to clean word boundaries
   (pause/sentence aware), scored by weighted features, optionally re-ranked
   by an LLM with grounded-hook checks (verbatim evidence, word overlap,
   numbers must appear). Works fully offline with no LLM. **Manual mode**:
   ClipForge writes transcript prompts (with `<<VOLUME +9 dB>>`,
   `<<CHAT 4x>>`, `<<GAME EVENT: wasted>>` marks) for you to paste into any
   AI; paste its JSON picks back and it renders those.
5. **Frame** — 18 layouts. The user's must-have: **select the facecam
   section and the gameplay section** (draggable boxes, presets,
   auto-detect). `auto` picks per clip: small live face → facecam stack,
   one big face → follow-face crop, several/none → blurred fill.
6. **Render** — FFmpeg 1080×1920, loudnorm −14 LUFS, 7 caption styles × 5
   animations, hook headline (ASS box or emoji title bar), watermark,
   music bed, news template, GTA wanted stars + mission card, paid-campaign
   banner (`@handle #ad`), kill-cam slow-mo replay, cover image, QC check
   sheet (3 frames), SRT/VTT/ASS sidecars, metadata JSON.
7. **Export** — per-job `POSTING.md` (caption to paste + hashtags + why +
   viral score + source position) and a ZIP with everything.

**Live mode**: rolling 6-second buffer with incremental transcription,
chat/schedule/spike triggers and a Clip-now button.

**Discover**: search YouTube for the biggest recent long streams on a
topic, plus your watchlist creators' newest videos (Settings → Watchlist).

## Feature map (where each feature came from)

| Feature | Source |
|---|---|
| Signals-first picking, works keyless | NeonClipping |
| Facecam + gameplay region select (draggable, presets, auto-detect) | NeonClipping |
| Grounded hooks (evidence + overlap + numbers) | NeonClipping |
| Audio-first fast path + section alignment | NeonClipping / stream-clips |
| Studio UI (sidebar, job list, clip grid, region editor) | PeakCut |
| Hinglish (Roman) captions | PeakCut |
| Live mode (buffer, triggers, clip-now) | PeakCut |
| Moment taxonomy (reaction/skill/funny/reveal/take/tip/story) | stream-clips |
| Signal marks in AI prompts (`<<VOLUME>>` `<<CHAT>>` `<<GAME EVENT>>`) | stream-clips |
| Pause-aware word-boundary snapping, min score | stream-clips |
| Auto framing (stack / follow / blur) + YuNet face tracks | stream-clips |
| Follow-face gliding crop | stream-clips |
| Chat-replay speed signal | stream-clips |
| Volume spikes vs local baseline | stream-clips |
| Two-model transcription (scan / captions) | stream-clips |
| Emoji title bar (Pillow) | stream-clips |
| QC check sheets (3 frames per clip) | stream-clips |
| POSTING.md + clips.json | stream-clips |
| Stream discovery + watchlist | stream-clips |
| Manual AI mode (prompts out, picks in) | stream-clips ("agent" brain) |
| Facecam memory per source | stream-clips |
| Friendly yt-dlp error messages | stream-clips |
| Game-event detection (WASTED/BUSTED/explosion/crash/chase) | Wanted |
| Viral score per clip | Wanted |
| Kill-cam slow-mo replay | Wanted |
| Wanted stars + mission card overlays | Wanted |
| Style presets (Heist, Retro 16-bit, Anime, Trailer, News, Viral) | Wanted |
| Paid-campaign mode (@handle + #ad burned in) | Wanted |

Deliberately **not** included: RP multi-angle (needs two synced sources —
too big for v1); CLI brains (claude/codex subprocess — the manual mode
covers no-key users); direct YouTube upload (stub in `publish.py` with
setup instructions — ClipForge stores no credentials by design).

## Configuration

Environment variables (see `.env.example`): `CLIPFORGE_HOST`,
`CLIPFORGE_PORT` (8780), `CLIPFORGE_DATA_DIR`, `CLIPFORGE_ASR_MODEL`,
`CLIPFORGE_LLM_BASE_URL` / `_MODEL` / `_KEY`, `CLIPFORGE_ENCODER`,
`CLIPFORGE_CPU=1` to force CPU Whisper. The Settings tab writes LLM
settings to `data/settings.json`.

## Honest limitations

- Game-event detection is heuristic (dark screen + centred text + tint).
  Without tesseract OCR installed, WASTED vs BUSTED is decided by red/blue
  tint and confidence is capped — treat it as a strong hint, not proof.
- Live sessions are in-memory: a restart loses session state (clips on
  disk remain). Live chat triggers cover Twitch IRC; YouTube live chat is
  not wired (chat *replay* speed is, for VODs).
- The follow/auto face tracking needs a real face on screen; synthetic or
  faceless content falls back to blurred fill.
- YouTube downloads can be bot-checked from datacenter IPs — the retry
  ladder helps; uploading a file always works.

## Layout of the code

```
app/
  server.py     FastAPI app + JSON API
  jobs.py       job store + pipeline orchestration
  media.py      yt-dlp ingest, probing, chat replay, discovery
  transcribe.py faster-whisper (cached, retry-safe)
  captions_yt.py borrowed YouTube captions
  signals.py    motion / loudness / spike / chat / game scan
  gameevents.py visual game-event detection (OpenCV)
  brain.py      picking, snapping, scoring, LLM judge, manual prompts
  excite.py     excitement lexicon
  facecam.py    border-profile detection + YuNet face tracks + auto look
  layouts.py    18 layout geometries + FFmpeg graphs
  captions.py   ASS/SRT/VTT generation (7 styles, 5 animations)
  titlecard.py  Pillow title bar / stars / mission card PNGs
  presets.py    style preset bundles
  render.py     per-clip render (loudnorm, overlays, kill-cam, check sheet)
  publish.py    metadata JSON, POSTING.md, ZIP export, upload stub
  live.py       live clipping sessions
  llm.py        optional OpenAI-compatible LLM client
web/            no-build HTML/CSS/JS studio UI
fonts/          caption fonts (OFL)
models/         YuNet face model (OpenCV zoo)
```
