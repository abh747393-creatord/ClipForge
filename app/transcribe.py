"""Local word-timed ASR with faster-whisper (GPU first, CPU fallback).

Reference: NeonClipping captions.py transcribe()/_load() (MIT). Words are
{"start","end","text"} in seconds relative to the requested window start.
"""
import hashlib
import json
import os
import threading
from pathlib import Path

from . import config, media, roman

_MODELS = {}
_LOCK = threading.Lock()
# One transcription at a time, process-wide. faster-whisper/ctranslate2 is not
# safe under concurrent transcribe() calls on the same model — the live ASR
# worker, live clip alignment and job workers used to race each other, which
# wedged clip renders at "rendering" forever on Windows. Serialize instead.
_TRANSCRIBE_LOCK = threading.Lock()

# Roman/Devanagari steering prompts (NeonClipping captions.py).
ROMAN_PROMPT = "Aaj hum GTA 6 ke baare mein baat karenge, yeh bahut mast hai. Kya haal hai bhai, chalo shuru karte hain. Matlab ekdum sahi hai, nahi yaar, bilkul."
DEVA_PROMPT = "यह हिंदी और अंग्रेज़ी में गेमिंग वीडियो है। GTA, गेमप्ले, क्लिपिंग, वीडियो, टूल।"


def _load_model(spec):
    """GPU first (int8_float16, with a tiny decode probe so a broken CUDA
    runtime falls back cleanly), CPU int8 otherwise. CLIPFORGE_CPU=1 (or
    NEONCLIPPING_CPU=1) forces CPU."""
    from faster_whisper import WhisperModel
    download_root = str(config.DATA_DIR / "models")
    force_cpu = os.environ.get("CLIPFORGE_CPU") == "1" or os.environ.get("NEONCLIPPING_CPU") == "1"
    has_cuda = False
    if not force_cpu:
        try:
            import ctranslate2
            has_cuda = ctranslate2.get_cuda_device_count() > 0
        except Exception:
            has_cuda = False
    if has_cuda:
        try:
            m = WhisperModel(spec, device="cuda", compute_type="int8_float16", download_root=download_root)
            import numpy as np
            list(m.transcribe(np.zeros(16000, dtype="float32"))[0])  # prove cuDNN/cuBLAS really load
            return m
        except Exception:
            pass
    return WhisperModel(spec, device="cpu", compute_type="int8",
                        cpu_threads=max(1, (os.cpu_count() or 4) - 2), download_root=download_root)


def _model_for(spec):
    """Load (once) and return the model for `spec`, with one retry.

    The first load may download the model; a transient failure mid-download
    (network hiccup, disk quota flake) can leave a partial cache that makes
    every later load fail the same way. On failure we drop the partial model
    directory for this spec and try once more before giving up."""
    with _LOCK:
        if spec in _MODELS:
            return _MODELS[spec]
        try:
            _MODELS[spec] = _load_model(spec)
        except Exception:
            _clear_partial_model(spec)
            _MODELS[spec] = _load_model(spec)  # second failure propagates
        return _MODELS[spec]


def _clear_partial_model(spec):
    """Remove a possibly-corrupt cached model dir for `spec` (best-effort)."""
    import shutil
    name = Path(str(spec)).name
    root = config.DATA_DIR / "models"
    for cand in root.glob(f"models--*--faster-whisper-{name}"):
        shutil.rmtree(cand, ignore_errors=True)
    direct = root / name
    if direct.is_dir() and not (direct / "model.bin").exists():
        shutil.rmtree(direct, ignore_errors=True)


def _collapse_repeats(words, limit=3):
    """Whisper sometimes loops on noisy audio ("chal chal chal chal..."). A
    word said more than `limit` times in a row is cut back to `limit`
    (Hindi really does say "bhaag bhaag bhaag", so three in a row stay)."""
    out, run, prev = [], 0, None
    for w in words:
        key = w["text"].lower().strip(".,!?;:")
        run = run + 1 if key == prev else 1
        prev = key
        if key and run > limit:
            continue
        out.append(w)
    return out


def transcribe_wav(wav_path, language=None, model=None, task="transcribe"):
    """Low-level helper: transcribe a 16 kHz mono WAV file.

    Returns (words, detected_language) with word times relative to the wav
    start. language="hinglish" transcribes as Hindi then transliterates to
    Roman letters; language="hi" stays Devanagari. task="translate" renders
    the speech as English (Whisper's translate task). Raises on model failure.
    """
    spec = model or config.ASR_MODEL
    m = _model_for(spec)
    audio, _sr = media.load_wav_mono(wav_path)
    whisper_lang = "hi" if language == "hinglish" else (language or None)
    prompt = DEVA_PROMPT if language in ("hi", "hinglish") else None
    # The lock covers the generator iteration too: m.transcribe() does its
    # real work lazily as segments are consumed. Concurrent calls on one
    # ctranslate2 model can deadlock — never let two run at once.
    with _TRANSCRIBE_LOCK:
        segments, info = m.transcribe(audio, beam_size=5, language=whisper_lang,
                                      task=task if task in ("transcribe", "translate") else "transcribe",
                                      initial_prompt=prompt, word_timestamps=True,
                                      vad_filter=True, condition_on_previous_text=False)
        words = []
        for segment in segments:
            for w in segment.words or []:
                text = (w.word or "").strip()
                if text and w.end > w.start:
                    words.append({"start": round(max(0.0, w.start), 3), "end": round(w.end, 3), "text": text})
    words = _collapse_repeats(words)
    if language == "hinglish":
        words = roman.words(words)
    return words, (info.language or whisper_lang or "")


def transcribe(src, start, end, work_dir, language=None, model=None, report=lambda m: None,
               task="transcribe") -> dict:
    """Transcribe src[start:end] → {"words": [Word relative to start], "language": str, "model": str}.

    Extracts a 16 kHz WAV into work_dir (deleted after), runs faster-whisper,
    and caches the result as work_dir/<key>.words.json keyed by
    (src stem, start, end, language, model, task); a cached result is returned
    without re-running the model. Raises on extraction/model failure.
    """
    src = Path(src)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    start, end = float(start), float(end)
    spec = model or config.ASR_MODEL
    model_tag = Path(str(spec)).name[:32]
    task = task if task in ("transcribe", "translate") else "transcribe"
    raw_key = f"{src.stem}|{start:.2f}|{end:.2f}|{language or 'auto'}|{model_tag}|{task}"
    key = hashlib.sha1(raw_key.encode("utf-8")).hexdigest()[:16]
    cached = work_dir / f"{key}.words.json"
    if cached.is_file():
        try:
            return json.loads(cached.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    report("Loading local speech model (first use may download it)")
    wav = work_dir / f"{key}.wav"
    media.extract_audio(src, wav, start=start, end=end, sr=16000)
    try:
        report("Transcribing locally")
        words, detected = transcribe_wav(wav, language=language, model=spec, task=task)
        dur = max(0.0, end - start)
        words = [dict(w, end=min(w["end"], dur)) for w in words if w["start"] < dur and w["end"] > w["start"]]
        result = {"words": words, "language": detected, "model": model_tag}
        try:
            cached.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return result
    finally:
        wav.unlink(missing_ok=True)
