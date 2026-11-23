# ClipForge Feature-Parity Audit — 2026-11-23

Sources: NeonClipping (iamajmeena/neonclipping), PeakCut (haziqahmedqasmi/peakcut),
stream-clips skill. Compared against ClipForge v1.1.4 code (app/ + web/).

Legend: YES = present, PARTIAL = partly present, NO = missing.

## Already present (YES) — verified in code
- Signals-first picking, works keyless (brain.py, signals.py, excite.py)
- Facecam + gameplay region select, presets, auto-detect, cam memory (facecam.py)
- Grounded hooks (evidence + overlap + numbers) (brain.py)
- Audio-first fast path + section alignment (jobs.py, media.find_offset)
- 18 layouts incl. all NeonClipping 11 + PeakCut 8 (layouts.py CATALOG)
- 5 aspects (9:16, 1:1, 4:5, 3:4, 16:9), 720/1080/2160, fps options
- 7 caption styles (PeakCut 3 + NeonClipping 4 adapted) (captions.py STYLES)
- 5 caption animations (highlight, pop_in, one_by_one, build_up, full_line)
- 4 hook looks (box, neon, banner, impact) (captions.HOOK_STYLES)
- Custom caption appearance + saved looks/profiles (capCustom UI, /api/profiles)
- Hinglish transliteration (roman.py), YouTube captions first (captions_yt.py)
- Two-model transcription scan/captions (options scan_model/caption_model)
- Live: rolling buffer, incremental ASR, Twitch chat, spike + schedule triggers,
  two-judge check (words+reaction), clip-now, hype meter, A/V align (live.py)
- Creators: add once, 60s watch, auto-record on live, persistence (live.py, v1.1.4)
- Persistence of jobs/live sessions across reload+restart (v1.1.4)
- Render semaphore + Whisper lock (v1.1.4), hardware encoder probe (render.py)
- Review-first candidate picking (review_first), manual range clip, transcript
  explorer + search, manual AI mode (prompts out/picks in)
- Word editor (edit words), subtitle import (SRT/ASS/VTT), AI post-text rewrite
- Watermark/logo, music bed + riser, fade, outro ribbon, news template,
  GTA stars/mission card, campaign banner, kill-cam, cover, QC sheet
- ZIP export, POSTING.md, metadata JSON, star ratings + learned weights
- Discover (topic search + watchlist), phone remote + PIN (/m)
- LLM: providers list, find models, test, Ollama/local + cloud (llm.py)
- Readiness checks, storage info, update CHECK, diagnostics (redacted)
- Discord/Telegram alerts (extras.notify), static-screen skip, game events
- Preview + real-frame preview (HLS/DASH), PO-token, CLIPFORGE_PROXY tunnel

## Gaps to implement (PARTIAL / NO)
### NeonClipping
1. `pop_in_line` caption animation — NO (ANIMS has 5 of 6)
2. Full caption-language list (52) in UI — PARTIAL (backend accepts any string;
   UI offers only Auto/en/hi/hinglish)
3. Hook display duration (2–60 s, default 8) — NO (hook shown whole clip)
4. Credit line overlay (manual or auto "Clip: channel") — NO
5. Background card (blur/colour/gradient/image + padding + rounded corners) — NO
6. Templates X light/dark + TikTok hook — PARTIAL (only "news")
7. Job queue: pause/resume, schedule (not_before), retry — NO (bare thread pool)
8. In-app updater APPLY (download zip, backup, replace, restart) — PARTIAL (check only)
9. Stuck-job watchdog (flag >15 min, log thread stacks) — NO
10. Vision review (3 frames → LLM vision → re-judge, optional) — NO
11. Editor keyboard shortcuts (I/O trim, Space play) — NO
12. Word editor split/merge + shift-all timings — PARTIAL (text edit only)
13. Import cache (reuse source download per URL+height) — NO
14. Per-creator saved options (layout/style/lang used for auto-record) — NO
15. Creator clip feed (clips per creator) — PARTIAL (past sessions list only)
16. Auto-live caps: 3 h max, disk guard, 30-min stop cooldown — NO
17. Support/donate links display — NO (minor)
18. Report: GitHub issue prefill link — PARTIAL (diagnostics download exists)

### PeakCut
19. Caption wording AI fix (spelling/numbers, identity-checked) — NO
20. English caption translation — NO
21. "Clip that" voice trigger + creator trigger phrases (instant clip) — NO
22. must_clip_min (force a clip if none in N minutes) — NO
23. Live stream embed panel (Twitch/YouTube player in UI) — NO
24. Creator profile enrichment (avatar, followers, live title/viewers) — NO
25. AI usage tracking (tokens per call/purpose + usage page) — NO
26. Load protection (busy/disk gate before heavy work) — NO
27. Chunked upload for large files — NO (single-shot only)
28. Daily digest alert (Discord/Telegram at set hour) — NO
29. Keep-awake while busy (Windows) — NO

### stream-clips
30. --new-only (continue from last processed position per URL) — NO
31. --repick (re-pick on cached transcript, keep downloads) — NO
32. Delete source audio after clips (disk hygiene) — PARTIAL (verify)

## Cannot implement here (with reasons)
- PeakCut multi-user accounts / admin dashboard / email / Google sign-in /
  PostgreSQL: ClipForge is a single-user local tool by design; this is a
  different product shape (auth + DB + mail stack), not a feature port.
- PeakCut platform auto-posting (YouTube/Instagram/TikTok OAuth, post queue,
  view tracking, earnings): needs the user's own platform developer apps +
  OAuth credentials and Google/Meta/TikTok app audits (uploads stay private
  until audited). No credentials are configured. publish.py keeps the
  documented YouTube-upload stub with setup instructions.
- PeakCut GPU ASR worker (separate machine): needs a second GPU machine.
  ClipForge's faster-whisper already uses local CUDA when present.
- PeakCut Arabic sherpa-onnx model: superseded — faster-whisper covers
  Arabic natively via the language option.
- NeonClipping legacy CLI live clipper (live/live_clip.py): superseded by
  in-app live mode.
- stream-clips CLI brains (claude/codex/gemini subprocess): deliberately
  excluded per ClipForge README; manual AI mode covers no-key users.
- NeonClipping report direct-send: needs a deployed collector endpoint the
  user does not have (diagnostics download + issue prefill implemented).
