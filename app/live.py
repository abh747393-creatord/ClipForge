"""ClipForge live clipping sessions.

One ffmpeg process per session does two jobs at once:
  * output 1: -c copy into 6-second MPEG-TS segments (a rolling ~20 min buffer)
  * output 2: 16 kHz mono PCM on stdout, read here second by second for
    loudness + rolling transcription (words on the session clock)

Segment timing: ffmpeg writes a CSV list per recorder part (name,start,end
relative to that part). Each part's session-clock base is the audio clock at
the moment the part started. This is an approximation — the segment clock and
the audio clock can drift a few seconds over an hour, and unlike PeakCut we
do not re-align clips by re-transcribing them (known gap).

Triggers (checked every 2 s by the monitor thread):
  * schedule: every ai_every_min minutes, clip the last ai_every_min*60+90 s
  * spike:    last-5 s loudness (linear) or last-8 s chat rate >= 1.6x the
              trailing 3-minute average -> clip spike-live_before .. spike+live_after
  * phrase:   someone says "clip (it|that|this)" -> clip word-60 .. word+15
Auto clips are rate-limited: >= 60 s apart, max live_per_hour per hour.
Manual "clip now" bypasses the rate limit.
"""
import math
import queue
import random
import re
import socket
import ssl
import subprocess
import threading
import time
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import brain, config, facecam, jobs, layouts, media, render, transcribe

SR = 16000
SEG_SEC = 6
BYTES_PER_SEC = SR * 2  # s16le mono
BUFFER_KEEP_SEC = 20 * 60
STALL_SEC = 90
MAX_RESTARTS = 3
TRANSCRIBE_EVERY_SEC = 7
CLIP_RE = re.compile(r"\bclip(\s+(it|that|this))?\b", re.IGNORECASE)
TWITCH_RE = re.compile(r"twitch\.tv/([A-Za-z0-9_]{3,25})", re.IGNORECASE)

SESSIONS: dict = {}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class LiveSession:
    def __init__(self, url: str, options: dict | None, store):
        self.id = uuid.uuid4().hex[:12]
        self.url = url
        self.options = jobs.clean_options(options)
        self.store = store
        self.status = "starting"
        self.started_at = time.time()
        self.title = ""
        self.error = None
        self.clips: list = []
        self.hype = 0

        self.dir = config.DATA_DIR / "live" / self.id
        self.buffer_dir = self.dir / "buffer"
        self.clips_dir = self.dir / "clips"
        self.buffer_dir.mkdir(parents=True, exist_ok=True)
        self.clips_dir.mkdir(parents=True, exist_ok=True)

        self.words: list = []        # {"start","end","text"} on the session clock
        self.loud_hist: list = []    # dB per second
        self.chat_hist: list = []    # messages per second (index = session second)
        self.segments: list = []     # (Path, start, end) on the session clock

        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._proc = None
        self._samples = 0
        self._pending = bytearray()          # PCM waiting for transcription
        self._pending_base = 0.0             # session second where _pending starts
        self._asr_queue: queue.Queue = queue.Queue()
        self._part = 0
        self._part_base: dict = {}           # part -> session second the part started
        self._csv_seen: dict = {}            # part -> rows already read
        self._seg_clock = 0.0
        self._last_seg_wall = time.time()
        self._restarts = 0
        self._clip_seq = 0
        self._last_auto_clip_wall = 0.0
        self._auto_clip_walls: list = []
        self._last_schedule_at = 0.0
        self._last_spike_at = -1e9
        self._phrase_seen = 0
        self._facecam_checked = False
        self._chat_on = False

    # ------------------------------------------------------------------ clock
    @property
    def audio_seconds(self) -> float:
        return self._samples / SR

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name=f"live-{self.id}").start()
        threading.Thread(target=self._transcribe_worker, daemon=True, name=f"live-asr-{self.id}").start()
        threading.Thread(target=self._monitor, daemon=True, name=f"live-mon-{self.id}").start()
        m = TWITCH_RE.search(self.url or "")
        if m:
            threading.Thread(target=self._chat, args=(m.group(1).lower(),),
                             daemon=True, name=f"live-chat-{self.id}").start()

    def stop(self) -> None:
        self._stop.set()
        self.status = "stopped"
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def state(self) -> dict:
        with self._lock:
            clips = [dict(c) for c in self.clips]
        return {
            "id": self.id,
            "url": self.url,
            "status": self.status,
            "started_at": _iso(self.started_at),
            "elapsed": round(self.audio_seconds),
            "clips": clips,
            "hype": int(self.hype),
            "error": self.error,
            "title": self.title,
        }

    # ------------------------------------------------------------------ ingest
    def _run(self) -> None:
        try:
            resolved = media.resolve_live(self.url)
            self.title = resolved.get("title") or ""
            urls = list(resolved.get("urls") or [])
            if not urls:
                raise RuntimeError("Could not resolve that live stream.")
        except Exception as e:  # noqa: BLE001
            self.status = "error"
            self.error = str(e)
            self._stop.set()
            return

        while not self._stop.is_set():
            proc = None
            try:
                proc = self._start_ffmpeg(urls)
                self.status = "recording"
                self._read_pcm(proc)
            except Exception as e:  # noqa: BLE001
                if not self._stop.is_set():
                    self.error = str(e)
            finally:
                if proc and proc.poll() is None:
                    try:
                        proc.terminate()
                    except OSError:
                        pass
            if self._stop.is_set():
                break
            self._restarts += 1
            if self._restarts > MAX_RESTARTS:
                self.status = "error"
                self.error = self.error or "The recorder kept stopping."
                self._stop.set()
                break
            self._stop.wait(3)
            try:  # stream addresses expire; resolve fresh ones for the restart
                resolved = media.resolve_live(self.url)
                urls = list(resolved.get("urls") or urls)
            except Exception:  # noqa: BLE001 - keep the old urls and let ffmpeg try
                pass
        if self.status == "recording":
            self.status = "stopped"

    def _start_ffmpeg(self, urls: list):
        self._part += 1
        part = self._part
        with self._lock:
            self._part_base[part] = self.audio_seconds
        prefix = f"p{part:02d}"
        a_in = "1:a:0" if len(urls) > 1 else "0:a:0"
        cmd = ["ffmpeg", "-v", "error", "-reconnect", "1", "-reconnect_streamed", "1",
               "-reconnect_delay_max", "10"]
        for u in urls:
            cmd += ["-i", u]
        cmd += ["-map", "0:v:0", "-map", a_in, "-c", "copy",
                "-f", "segment", "-segment_time", str(SEG_SEC), "-reset_timestamps", "1",
                "-segment_format", "mpegts", "-segment_list", f"{prefix}.csv",
                "-segment_list_type", "csv", f"{prefix}_%05d.ts",
                "-map", a_in, "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"]
        errlog = open(self.dir / "ffmpeg.log", "ab")
        proc = subprocess.Popen(cmd, cwd=self.buffer_dir, stdout=subprocess.PIPE, stderr=errlog)
        with self._lock:
            self._proc = proc
        return proc

    def _read_pcm(self, proc) -> None:
        """Read the PCM pipe second by second: loudness + transcription chunks."""
        buf = bytearray()
        stream = proc.stdout
        while not self._stop.is_set():
            try:
                chunk = stream.read1(65536)
            except (OSError, ValueError):
                break
            if not chunk:
                break  # ffmpeg ended
            buf += chunk
            while len(buf) >= BYTES_PER_SEC:
                one_sec = bytes(buf[:BYTES_PER_SEC])
                del buf[:BYTES_PER_SEC]
                self._on_second(one_sec)
        self._flush_pending()

    def _on_second(self, pcm: bytes) -> None:
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
        db = 20.0 * math.log10(rms + 1e-9)
        with self._lock:
            if not self._pending:
                self._pending_base = self.audio_seconds
            self._pending += pcm
            self._samples += SR
            self.loud_hist.append(round(db, 1))
            ready = len(self._pending) >= TRANSCRIBE_EVERY_SEC * BYTES_PER_SEC
            if ready:
                data, base = bytes(self._pending), self._pending_base
                self._pending = bytearray()
        if ready:
            self._asr_queue.put((data, base))

    def _flush_pending(self) -> None:
        with self._lock:
            if self._pending:
                self._asr_queue.put((bytes(self._pending), self._pending_base))
                self._pending = bytearray()

    # ------------------------------------------------------------------ transcription
    def _transcribe_worker(self) -> None:
        seq = 0
        while not self._stop.is_set():
            try:
                pcm, base = self._asr_queue.get(timeout=2)
            except queue.Empty:
                continue
            seq += 1
            wav = self.buffer_dir / f"chunk_{seq:05d}.wav"
            try:
                with wave.open(str(wav), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(SR)
                    w.writeframes(pcm)
                lang = self.options.get("language")
                if self.options.get("hinglish") and not lang:
                    lang = "hinglish"
                words, _lang = transcribe.transcribe_wav(wav, language=lang)
                shifted = [{"start": base + float(w["start"]), "end": base + float(w["end"]),
                            "text": w["text"]} for w in (words or [])]
                with self._lock:
                    self.words.extend(shifted)
                    self.words.sort(key=lambda w: w["start"])
            except Exception:  # noqa: BLE001 - a failed chunk loses a few words, not the session
                pass
            finally:
                wav.unlink(missing_ok=True)

    # ------------------------------------------------------------------ segments
    def _scan_segments(self) -> None:
        with self._lock:
            for part in range(1, self._part + 1):
                csv = self.buffer_dir / f"p{part:02d}.csv"
                if not csv.exists():
                    continue
                try:
                    rows = [r.split(",") for r in csv.read_text(encoding="utf-8", errors="ignore").splitlines()
                            if r.count(",") >= 2]
                except OSError:
                    continue
                fresh = rows[self._csv_seen.get(part, 0):]
                base = self._part_base.get(part, 0.0)
                for name, a, b in (r[:3] for r in fresh):
                    try:
                        start, end = base + float(a), base + float(b)
                    except ValueError:
                        continue
                    if end <= start:
                        continue
                    self.segments.append((self.buffer_dir / name, start, end))
                    self._seg_clock = max(self._seg_clock, end)
                    self._last_seg_wall = time.time()
                self._csv_seen[part] = len(rows)
            keep_from = self._seg_clock - BUFFER_KEEP_SEC
            while self.segments and self.segments[0][2] < keep_from:
                old = self.segments.pop(0)[0]
                try:
                    old.unlink(missing_ok=True)
                except OSError:
                    pass

    # ------------------------------------------------------------------ Twitch chat (best-effort)
    def _chat(self, channel: str) -> None:
        fails = 0
        while not self._stop.is_set() and fails < 5:
            try:
                raw = socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=20)
                sock = ssl.create_default_context().wrap_socket(raw, server_hostname="irc.chat.twitch.tv")
                nick = f"justinfan{random.randint(10000, 99999)}"
                sock.sendall(f"NICK {nick}\r\nJOIN #{channel}\r\n".encode())
                sock.settimeout(30)
                self._chat_on = True
                fails = 0
                rest = b""
                while not self._stop.is_set():
                    try:
                        data = sock.recv(8192)
                    except socket.timeout:
                        continue
                    if not data:
                        break
                    *lines, rest = (rest + data).split(b"\r\n")
                    for line in lines:
                        if line.startswith(b"PING"):
                            sock.sendall(b"PONG :tmi.twitch.tv\r\n")
                        elif b" PRIVMSG #" in line:
                            sec = int(self.audio_seconds)
                            with self._lock:
                                while len(self.chat_hist) <= sec:
                                    self.chat_hist.append(0)
                                self.chat_hist[sec] += 1
                sock.close()
            except Exception:  # noqa: BLE001 - chat is a bonus signal, never fatal
                fails += 1
            self._chat_on = False
            self._stop.wait(5)

    # ------------------------------------------------------------------ triggers
    def _monitor(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(2)
            if self._stop.is_set():
                break
            try:
                self._scan_segments()
                self._check_stall()
                self._update_hype()
                self._check_triggers()
            except Exception:  # noqa: BLE001 - the monitor must keep monitoring
                pass

    def _check_stall(self) -> None:
        if self.status != "recording":
            return
        if time.time() - self._last_seg_wall > STALL_SEC and self._proc and self._proc.poll() is None:
            # no new picture for 90 s: kill ffmpeg, the ingest loop restarts it
            # (and counts the restart; > MAX_RESTARTS ends the session in error)
            try:
                self._proc.terminate()
            except OSError:
                pass
            self._last_seg_wall = time.time()  # give the restart time to produce segments

    def _update_hype(self) -> None:
        """Hype 0-100. With >= 60 s of history it is relative: the last-5 s
        loudness (linear) and last-8 s chat rate against their trailing
        3-minute averages — ratio 0.5 -> 0, ratio 2.0 -> 100, take the max.
        Before that, an absolute loudness scale: -45 dB -> 0, -10 dB -> 100."""
        with self._lock:
            loud = list(self.loud_hist)
            chat = list(self.chat_hist)
        if len(loud) < 5:
            self.hype = 0
            return
        if len(loud) < 60:
            recent = sum(loud[-5:]) / 5.0
            self.hype = int(max(0, min(100, round((recent + 45.0) / 35.0 * 100))))
            return
        amp = [10 ** (db / 20.0) for db in loud]
        loud_now = sum(amp[-5:]) / 5.0
        loud_base = sum(amp[-185:-5]) / max(1, len(amp[-185:-5]))
        scores = []
        if loud_base > 0:
            scores.append((loud_now / loud_base - 0.5) / 1.5 * 100)
        if len(chat) >= 60:
            chat_now = sum(chat[-8:]) / 8.0
            chat_base = sum(chat[-188:-8]) / max(1, len(chat[-188:-8]))
            if chat_base > 0:
                scores.append((chat_now / chat_base - 0.5) / 1.5 * 100)
        self.hype = int(max(0, min(100, round(max(scores))))) if scores else 0

    def _check_triggers(self) -> None:
        now = self.audio_seconds
        opts = self.options
        every = int(opts.get("ai_every_min") or 3)

        # schedule: every ai_every_min minutes over the last window + 90 s overlap
        if now - self._last_schedule_at >= every * 60 and now > 60:
            self._last_schedule_at = now
            self._make_clip(max(0.0, now - (every * 60 + 90)), now, "schedule")

        # spike: loudness or chat jumps 1.6x over the trailing 3-minute average
        with self._lock:
            loud = list(self.loud_hist)
            chat = list(self.chat_hist)
        if len(loud) >= 190 and now - self._last_spike_at >= 60:
            amp = [10 ** (db / 20.0) for db in loud]
            loud_now = sum(amp[-5:]) / 5.0
            loud_base = sum(amp[-185:-5]) / max(1, len(amp[-185:-5]))
            spiked = loud_base > 0 and loud_now >= 1.6 * loud_base
            if not spiked and len(chat) >= 190:
                chat_now = sum(chat[-8:]) / 8.0
                chat_base = sum(chat[-188:-8]) / max(1, len(chat[-188:-8]))
                spiked = chat_base > 0 and chat_now >= 1.6 * chat_base
            if spiked:
                self._last_spike_at = now
                spike_t = max(0.0, now - 2)
                self._make_clip(spike_t - float(opts.get("live_before") or 24),
                                spike_t + float(opts.get("live_after") or 14), "spike")

        # phrase: someone said "clip (it|that|this)"
        with self._lock:
            new_words = self.words[self._phrase_seen:]
            self._phrase_seen = len(self.words)
        if new_words:
            text = " ".join(w["text"] for w in new_words)
            m = CLIP_RE.search(text)
            if m:
                # find the word the match starts in, for its session-clock time
                pos = 0
                for w in new_words:
                    if pos <= m.start() < pos + len(w["text"]) + 1:
                        t = float(w["start"])
                        self._make_clip(t - 60, t + 15, "phrase")
                        break
                    pos += len(w["text"]) + 1

    # ------------------------------------------------------------------ clip making
    def _rate_limited(self) -> bool:
        now_wall = time.time()
        if now_wall - self._last_auto_clip_wall < 60:
            return True
        cap = int(self.options.get("live_per_hour") or 4)
        recent = [t for t in self._auto_clip_walls if now_wall - t < 3600]
        self._auto_clip_walls = recent
        return len(recent) >= cap

    def _make_clip(self, a: float, b: float, trigger: str) -> None:
        a, b = max(0.0, float(a)), max(float(a) + 5.0, float(b))
        if trigger != "manual":
            if self._rate_limited():
                return
            self._last_auto_clip_wall = time.time()
            self._auto_clip_walls.append(self._last_auto_clip_wall)
        threading.Thread(target=self._make_clip_work, args=(a, b, trigger),
                         daemon=True, name=f"live-clip-{self.id}").start()

    def clip_now(self, seconds: float = 30) -> None:
        now = self.audio_seconds
        if self._seg_clock and self._seg_clock < now:
            now = self._seg_clock  # clip what the picture has actually recorded
        if now < 5:
            raise ValueError("Nothing has been recorded yet.")
        self._make_clip(now - float(seconds), now, "manual")

    def _make_clip_work(self, a: float, b: float, trigger: str) -> None:
        with self._lock:
            self._clip_seq += 1
            seq = self._clip_seq
        name = f"live{seq:02d}"
        clip = {"name": name, "start": round(a, 2), "end": round(b, 2), "score": 0,
                "title": "", "hook": "", "description": "", "hashtags": [],
                "highlight_words": [], "reason": trigger, "kind": trigger,
                "features": {}, "status": "rendering", "error": None,
                "facecam": self.options.get("facecam"), "game": self.options.get("game")}
        with self._lock:
            self.clips.append(clip)
        try:
            # wait (<= 60 s) until the buffer covers [a, b] — spike/phrase
            # windows reach a few seconds into the future
            deadline = time.time() + 60
            while time.time() < deadline and not self._stop.is_set():
                self._scan_segments()
                with self._lock:
                    covered = bool(self.segments) and self.segments[-1][2] >= b
                if covered or self.status != "recording":
                    break
                time.sleep(1)

            with self._lock:
                segs = [s for s in self.segments if s[2] > a and s[1] < b and s[0].exists()]
            if not segs or segs[0][1] > a + 2:
                raise RuntimeError("That part of the stream is no longer in the buffer.")
            seg0_time = segs[0][1]

            lst = self.clips_dir / f"{name}.txt"
            lst.write_text("".join(f"file '{s[0].as_posix()}'\n" for s in segs), encoding="utf-8")
            src = self.clips_dir / f"{name}.src.ts"
            try:
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                                "-i", str(lst), "-c", "copy", str(src)],
                               check=True, capture_output=True, timeout=300)
            finally:
                lst.unlink(missing_ok=True)

            pr = media.probe(src)
            src_w, src_h = int(pr.get("width") or 0), int(pr.get("height") or 0)
            src_dur = float(pr.get("duration") or (segs[-1][2] - seg0_time))

            if layouts.needs_cam(self.options.get("layout")) and not self.options.get("facecam") \
                    and not self._facecam_checked:
                self._facecam_checked = True
                try:
                    det = facecam.detect(src, src_dur, src_w, src_h)
                    if det:
                        self.options["facecam"] = {"x": det["x"], "y": det["y"], "w": det["w"], "h": det["h"]}
                        clip["facecam"] = self.options["facecam"]
                except Exception:  # noqa: BLE001 - detection is best-effort
                    pass

            with self._lock:
                words_snapshot = list(self.words)
            win_rel = [{"start": float(w["start"]) - a, "end": float(w["end"]) - a, "text": w["text"]}
                       for w in words_snapshot if a <= float(w["start"]) <= b]
            try:
                desc = brain.describe(win_rel, fallback_title=self.title)
            except Exception:  # noqa: BLE001 - titles are best-effort
                desc = {}
            for k in ("title", "hook", "description", "hashtags", "highlight_words"):
                if desc.get(k):
                    clip[k] = desc[k]

            # render wants words on the concat file's timeline:
            # file_time = session_time - seg0_time (segment clock; see module note on drift)
            words_src = [{"start": max(0.0, float(w["start"]) - seg0_time),
                          "end": max(0.0, float(w["end"]) - seg0_time), "text": w["text"]}
                         for w in words_snapshot
                         if float(w["end"]) > seg0_time and float(w["start"]) < seg0_time + src_dur]
            clip_for_render = dict(clip, start=a - seg0_time, end=b - seg0_time)
            result = render.render_clip(src, self.clips_dir, name, clip_for_render,
                                        words_src, src_w, src_h, self.options)
            clip.update(jobs._basename_fields(result))
            clip["status"] = "done"
            clip["error"] = None
        except Exception as e:  # noqa: BLE001 - one failed clip must not stop the session
            clip["status"] = "error"
            clip["error"] = str(e)


# ---------------------------------------------------------------------- module API
def start_session(url: str, options: dict | None, store) -> LiveSession:
    session = LiveSession(url, options, store)
    SESSIONS[session.id] = session
    session.start()
    return session


def get_session(session_id: str):
    return SESSIONS.get(session_id)


def stop_session(session_id: str):
    session = SESSIONS.get(session_id)
    if session:
        session.stop()
    return session
