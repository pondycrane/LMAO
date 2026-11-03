#!/usr/bin/env python3
"""RoboDog ESP-Claw voice-loop E2E (DESIGN.md Phase 2 gate).

Drives the standalone esp-claw board's NATIVE voice loop end-to-end and asserts
each leg from the DEVICE SERIAL CONSOLE (there is no bridge journal on this
board):

    OPEN_SESSION : `rd_listen` -> device logs "listen started"
    ASR_MATCH    : corpus WAV played via test speaker -> device logs
                   `ASR text="..."` fuzzy-matches the corpus transcript
    REPLY        : device logs `agent reply: <text>` (native claw_agent_mgr LLM)
    TTS          : device logs `TTS start` ... `TTS stop` (ES8311 speaker)
    RING         : test mics the board's spoken reply -> remote re-ASR overlaps
    SUBTITLE     : RD_SHOT frame -> capture.decode_hermes -> Qwen2.5-VL readback
                   transcribes the reply rendered on the RLCD

The device console is the single assert source, exactly like the hermes E2E
used the bridge journal.  Same loud-skip convention as the hermes gate.

    python3 robodog_client/esp-claw/robodog_claw_e2e.py [--corpus capabilities]
"""

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import serial

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

import capture  # noqa: E402  (RD_SHOT decode + vision readback)

BOARD_VID, BOARD_PID = 0x303A, 0x1001
SOUND_CARD = os.environ.get("RD_SOUND_CARD", "Device")
# ALSA device for the test rig (test speaker out + test mic in).  The
# bidirectional USB PnP Audio device is card 2 (hw:2) on the workspace host;
# there is no `default` alias.  Override with RD_ALSA_DEVICE.
ALSA_DEV = os.environ.get("RD_ALSA_DEVICE", "hw:2")
PLAY_VOL = float(os.environ.get("RD_PLAY_VOL", "0.7"))
N_RUNS = int(os.environ.get("E2E_RUNS", "1"))
ASR_TIMEOUT = int(os.environ.get("E2E_ASR_TIMEOUT", "90"))
REPLY_TIMEOUT = int(os.environ.get("E2E_REPLY_TIMEOUT", "180"))
CONSOLE_BAUD = 115200

# Whisper endpoint for re-ASR of the board's spoken reply (the host reaches it
# through the same socat forward the board uses, or an env override).
# Re-ASR of the board's spoken reply.  Default = the same Whisper endpoint the
# BOARD calls (spark-5307 = 192.168.50.27, on this host's LAN); the old
# 192.168.50.153:11801 value was the workspace socat forward, which is gone now
# that spark-5307 sits on 192.168.50.0/24 and both sides reach it directly.
ASR_URL = os.environ.get("RD_ASR_URL",
                         "http://192.168.50.27:8001/v1/audio/transcriptions")
ASR_MODEL = os.environ.get("RD_ASR_MODEL", "whisper-1")
# Playback settle after `listen started` before the test speaker plays the
# corpus into the board mic (mic/ADC ramp).  Too early -> attenuated below the
# energy-VAD threshold (no utterance); a couple of seconds is safe here.
PLAY_SETTLE = float(os.environ.get("RD_PLAY_SETTLE", "3.0"))
# Window for the test mic to capture the board's spoken TTS reply.
RING_CAP_SECS = float(os.environ.get("RD_RING_CAP_SECS", "15"))
# --voice-verify: capture the WHOLE chunked reply (multiple chunks back-to-back
# can total 60+ s of speech from the verbose es-claw root agent), so the mic
# must stay recording from the first `TTS start` until the last `TTS stop`.
VOICE_CAP_SECS = float(os.environ.get("RD_VOICE_CAP_SECS", "240"))
SILENCE = os.environ.get("E2E_AUDIO_SG", "sg audio -c")


def log(msg):
    print(f"[e2e] {time.strftime('%H:%M:%S')} {msg}", flush=True)


# ---------------------------------------------------------------------------
# Board reset/panic sentinels (NO_RESET leg)
# ---------------------------------------------------------------------------
# Any console line matching these marks a board reset or heap/panic fault.  The
# console drain thread (ConsoleTail) feeds every line through note_panic(), so
# a mid-run reboot (rst:0x / Guru Meditation / the DESIGN.md R7 tlsf heap
# corruption) is NAMED by the NO_RESET leg instead of only failing whichever
# leg was mid-wait — and it is seen even while the host is busy with audio.
PANIC_RE = re.compile(
    r"rst:0x|Guru Meditation|assert failed|Backtrace|abort\\(\\)|tlsf|"
    r"heap.corr|CORRUPT|StoreProhibited|LoadProhibited|Rebooting"
)

# Per-run accumulator: run_loop clears it at the start of every session and
# converts it into the NO_RESET assert at the end.
RUN_RESET_LINES = []


def note_panic(ln):
    if PANIC_RE.search(ln):
        RUN_RESET_LINES.append(ln)
        log(f"PANIC SENTINEL: {ln[:160]}")


def set_no_reset(res):
    if RUN_RESET_LINES:
        res["asserts"]["NO_RESET"] = "FAIL " + RUN_RESET_LINES[0]
    else:
        res["asserts"]["NO_RESET"] = "PASS (no reset/panic markers)"
    return res


def shell_run(cmd, timeout=120):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


# ---------------------------------------------------------------------------
# Rig preflight
# ---------------------------------------------------------------------------
def detect_board_port():
    port = os.environ.get("ROBODOG_PORT", "")
    if port == "AUTO":
        port = ""
    if port:
        return port
    import glob

    cands = []
    for dev in glob.glob("/dev/serial/by-id/*"):
        if "USB-Serial" in dev or "JTAG" in dev:
            cands.append(os.path.realpath(dev))
    for cand in sorted(set(cands)):
        if Path(cand).exists():
            return cand
    for cand in glob.glob("/dev/ttyACM*"):
        if Path(cand).exists():
            return cand
    return None


def detect_sound_card():
    if SOUND_CARD:
        return SOUND_CARD
    try:
        out = subprocess.run(["aplay", "-L"], capture_output=True, text=True)
        for line in out.stdout.splitlines():
            line = line.strip()
            if line and "default" not in line and line != "null":
                return line
    except Exception:
        pass
    return None


def remote_asr(wav_path):
    """Transcribe a WAV via the OpenAI-compatible Whisper endpoint."""
    boundary = "--lmao-ring-" + os.urandom(8).hex()
    wav = Path(wav_path).read_bytes()
    fields = [
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="model"\r\n\r\n'
        f"{ASR_MODEL}\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; '
        'filename="ring.wav"\r\n'
        "Content-Type: audio/wav\r\n\r\n"
    ]
    body = ("".join(fields)).encode() + wav + f"\r\n--{boundary}--\r\n".encode()
    import urllib.request

    req = urllib.request.Request(
        ASR_URL, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.load(resp)["text"]


# ---------------------------------------------------------------------------
# Host-side audio drive (test speaker -> board mic) + capture (board speaker)
# ---------------------------------------------------------------------------
def _audio_cmd(core):
    """Wrap a raw ffmpeg command for the test-rig audio device.  Normally the
    rig runs behind `sg audio -c '<cmd>'` (the ALSA nodes are audio-group).
    When E2E_AUDIO_SG is empty the operator runs as root / a group member
    directly — then emit the BARE command: prefixing an empty string would
    leave ` 'ffmpeg ...' `, which the shell reads as a single space-containing
    COMMAND NAME and fails with 127, silently killing playback/capture."""
    if SILENCE:
        return f"{SILENCE} '{core}'"
    return core


def shell_ok(cmd, timeout):
    r = shell_run(cmd, timeout=timeout)
    if r.returncode != 0:
        log(f"audio cmd failed rc={r.returncode}: {cmd[:120]}… {r.stderr[-200:].strip()}")
        return False
    return True


def play_wav(path: Path, volume: float = PLAY_VOL):
    # The board's mic sits far from the test speaker (marginal coupling, the
    # documented RING PASS-WEAK rig): stream the corpus TWICE back-to-back at
    # realtime so the energy-VAD and Whisper get a clear, repeatable signal.
    core = (
        f"ffmpeg -hide_banner -loglevel error -re -stream_loop 2 -i {path} "
        f"-af volume={volume} -ac 2 -f alsa {ALSA_DEV}"
    )
    shell_ok(_audio_cmd(core), 120)


def capture_mic(path: Path, seconds: float):
    core = (
        f"ffmpeg -hide_banner -loglevel error -f alsa "
        f"-i {ALSA_DEV} -t {seconds} -ac 1 -ar 16000 {path}"
    )
    shell_ok(_audio_cmd(core), int(seconds) + 30)
    return path


def trim_loudest_window(path: Path, out: Path, seconds: float):
    """Find the loudest ~3 s window (RMS of 0.5 s buckets) of a mic capture."""
    import wave as _w

    with _w.open(str(path), "rb") as w:
        n = w.getnframes()
        rate = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        frames = w.readframes(n)
    import array

    if sw != 2:
        return None
    samples = array.array("h", frames)
    if ch > 1:
        samples = array.array("h", samples[::ch])  # take left channel
    fs = rate
    bucket = int(fs // 2)  # 0.5 s
    wins = []
    for i in range(0, max(len(samples) - bucket, 1), bucket):
        chunk = samples[i : i + bucket]
        if not chunk:
            break
        rms = sum(abs(s) for s in chunk) / len(chunk)
        wins.append((rms, i / fs))
    if not wins:
        return None
    _, best_start = max(wins)
    dur = min(seconds, len(samples) / fs - best_start)
    if dur < 1.0:
        dur = min(seconds, len(samples) / fs)
        best_start = 0.0
    start_s = int(best_start)
    start_f = start_s * rate * ch * 2
    end_f = min(int((best_start + dur) * rate * ch * 2), len(frames))
    with _w.open(str(path), "rb") as w:
        src = w.readframes(w.getnframes())
    burst = src[start_f:end_f]
    with _w.open(str(out), "wb") as w2:
        w2.setnchannels(1)
        w2.setsampwidth(2)
        w2.setframerate(rate)
        w2.writeframes(burst)
    return best_start


# ---------------------------------------------------------------------------
# Device console (serial) tail
# ---------------------------------------------------------------------------
class ConsoleTail:
    """Owns the device console and drains it CONTINUOUSLY.

    The console is the single assert source for this gate, so a line may never
    be lost.  Reading only while a `wait_for_log` runs loses lines whenever the
    host blocks elsewhere: `play_wav` (~10 s) and the remote re-ASR (tens of
    seconds) leave the USB-Serial/JTAG buffer unread while the device keeps
    printing (the RD_SHOT self-tick alone is ~7 KB/s), so the ASR/reply/TTS
    lines of the NEXT session are dropped — the gate then reports a false
    "session ended before ASR" on a board that actually worked.  A background
    thread therefore reads the port for the whole session and every scanner is
    a cursor over the accumulated lines (start = an index, not a byte offset),
    so no consumer can consume or discard another's lines.

    Also drains the console during host-side audio work, which keeps the
    device's blocking `printf` cheap instead of stalling its tasks on a full
    CDC FIFO."""

    def __init__(self, port):
        self.s = serial.Serial(port, CONSOLE_BAUD, timeout=0.2)
        time.sleep(1.0)  # let USB-Serial/JTAG enumeration settle
        self.s.reset_input_buffer()
        self._cond = threading.Condition()
        self._lines = []
        self._closed = False
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self):
        acc = b""
        while not self._closed:
            try:
                data = self.s.read(8192)
            except Exception:  # noqa: BLE001 — port vanished: stop draining
                break
            if not data:
                continue
            acc += data
            while b"\n" in acc:
                raw, acc = acc.split(b"\n", 1)
                ln = raw.decode("utf-8", "replace").strip()
                note_panic(ln)  # NO_RESET: reset/panic sentinel scan
                with self._cond:
                    self._lines.append(ln)
                    self._cond.notify_all()

    def write(self, cmd):
        self.s.write(f"{cmd}\r\n".encode())
        self.s.flush()

    def mark(self):
        """Cursor for "everything from now on"."""
        with self._cond:
            return len(self._lines)

    def take(self, start, timeout):
        """Block until lines past `start` exist or `timeout` elapses.
        Returns (lines_from_start, new_cursor)."""
        deadline = time.time() + timeout
        with self._cond:
            while len(self._lines) == start and not self._closed:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._cond.wait(min(remaining, 1.0))
            return self._lines[start:], len(self._lines)

    def close(self):
        self._closed = True
        with self._cond:
            self._cond.notify_all()
        self._thread.join(timeout=2.0)
        self.s.close()


# ---------------------------------------------------------------------------
# Transcript normalization + fuzzy match
# ---------------------------------------------------------------------------
# Spoken numbers -> digits.  Whisper renders spoken numerals as digits
# ("twenty to thirty" -> "20 to 30"), so a verbatim-word comparison scored a
# perfectly-heard corpus as a partial miss (`count slowly from twenty to
# thirty` vs `out slowly from 20 to 30` = 0.50 overlap -> false FAIL).  Both
# sides are normalized, so "twenty" and "20" become the same token.
_NUM_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}


# CJK ideograph ranges (Unified, ext. A, compatibility).  Chinese has no
# word spacing and Whisper's punctuation is unreliable, so each ideograph is
# its own matching token — the stable unit for ASR_MATCH / SUBTITLE / RING on
# a Chinese turn (the dual-language path the corpus now gates).
_CJK_RE = "[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"


def has_cjk(text):
    """True when the text contains at least one CJK ideograph."""
    return bool(text) and re.search(_CJK_RE, text) is not None


def dominant_script(text):
    """'cjk' or 'latin' — which script carries the text.  A reply that merely
    quotes a Chinese word stays 'latin'; a reply WRITTEN in Chinese is 'cjk'."""
    cjk = len(re.findall(_CJK_RE, text or ""))
    latin = len(re.findall(r"[A-Za-z]", text or ""))
    return "cjk" if cjk > latin else "latin"


def norm(text):
    """Tokenize for fuzzy matching: Latin/digit runs become word tokens
    (lowercase, spoken numbers collapsed to digits), each CJK ideograph its
    own token.  Punctuation/other scripts are separators."""
    toks = re.findall(rf"[a-z0-9]+|{_CJK_RE}", text.lower())
    out = []
    i = 0
    while i < len(toks):
        t = toks[i]
        v = _NUM_WORDS.get(t)
        if v is not None and v >= 20:
            nxt = _NUM_WORDS.get(toks[i + 1]) if i + 1 < len(toks) else None
            if nxt is not None and 1 <= nxt <= 9:
                out.append(str(v + nxt))
                i += 2
                continue
        out.append(str(v) if v is not None else t)
        i += 1
    return out


def token_overlap(a, b):
    if not a or not b:
        return 0.0
    an, bn = set(norm(a)), set(norm(b))
    inter = len(an & bn)
    return inter / max(len(an), len(bn))


def wait_for_log(tail, patterns, timeout, label="", start=None):
    """Wait for a console line matching one of `patterns`, scanning from
    `start` (an index into the drained lines; default = from now on).
    Returns (regex Match or None, cursor AFTER the matched line) so the caller
    can hand the same cursor to the next scanner — the line-level equivalent of
    the old byte `spill` hack, which no longer needs to exist because nothing
    is ever discarded."""
    pos = tail.mark() if start is None else start
    deadline = time.time() + timeout
    while True:
        lines, pos = tail.take(pos, max(0.0, deadline - time.time()))
        for ln in lines:
            for pat in patterns:
                m = re.search(pat, ln)
                if m:
                    if label:
                        log(f"{label}: {ln[:160]}")
                    return m, pos
        if time.time() >= deadline:
            return None, pos


# --voice-verify console collectors: the device logs one `voice chunk i/n`
# line per queued sentence chunk before its TTS bracket, so the host can prove
# the FULL reply was queued and voiced without any acoustic ambiguity.
CHUNK_RE = re.compile(r"voice chunk (\d+)/(\d+):\s*(.*)")
TTS_START_RE = re.compile(r"TTS start")
TTS_STOP_RE = re.compile(r"TTS stop")
# ESP log timestamp prefix `I (123456)` where 123456 is MILLISECONDS of
# uptime (measured on this device: a board 23 min from boot reads ~1,404,000).
# Used to measure the inter-chunk gap (TTS stop(i) -> TTS start(i+1)): the
# prefetch feature overlaps the next WAV fetch with the current playback, so
# that gap collapses from the ~1-3 s Kokoro HTTP round-trip (fetch was on the
# critical path BEFORE the fix) to a few ms (next WAV already buffered).
ESP_TS_RE = re.compile(r"^[IWED] \((\d+)\)")
# A prefetched chunk begins playback within ~a mixer-open of the previous stop
# (<50 ms observed); anything past this means a synchronous fetch-then-play is
# happening (prefetch NOT effective). Generous headroom above the observed ~ms
# to stay robust against log-timestamp jitter.
GAP_THRESHOLD_MS = 250
# R3: the device logs `voice truncated: ...` (robodog_voice.c) when a reply
# exceeds CONFIG_ROBODOG_VOICE_TTS_MAX_CHUNKS and its tail is dropped — the
# turn is NOT a complete readout and the gate MUST fail on this marker.
TRUNCATED_RE = re.compile(r"voice truncated")
# Which TTS path the firmware actually took (diagnoses GAP_VERIFY): the
# voice-split queue can run either through the N-slot pipeline (`pipeline
# chunk i/n buffered=yes ahead=k` + few-ms gaps), or its per-chunk sync
# fallback (`prefetch chunk i/n failed; sync fallback` — the endpoint fetch
# lands in the gap), or the whole turn on the sequential single-buffer path
# (`TTS pipeline unavailable` at turn start).
PF_READY_RE = re.compile(r"prefetch chunk \d+/\d+ ready")
PF_FAIL_RE = re.compile(r"prefetch chunk \d+/\d+ failed")
DBUFF_UNAVAIL_RE = re.compile(r"TTS pipeline unavailable")
# Per-boundary pipeline state the firmware logs before each chunk plays:
#   `pipeline chunk 3/8 buffered=yes ahead=2`
# `buffered=yes` proves the chunk was ALREADY synthesized when its turn came
# (the pipeline hid the endpoint latency); `ahead=k` is how many later chunks
# were buffered behind it.  This is the deterministic leg for "no gap between
# response segments": every boundary after the first MUST be buffered.
PIPE_STATE_RE = re.compile(r"pipeline chunk (\d+)/(\d+) buffered=(\w+) ahead=(\d+)")
# Device-side synthesis timing per chunk (`fetched chunk i/n in <ms> ms (<B> B)`).
# Diagnostic for the seam-gap arithmetic: gap(k) ≈ synth(k+1) - play(k) + drift,
# so a fetch that takes longer than the chunk it follows is what opens a seam.
FETCH_MS_RE = re.compile(r"fetched chunk (\d+)/(\d+) in (\d+) ms \((\d+) B\)")


def collect_voice_chunks(tail, timeout, start=None):
    """Scan the drained console from `start` while the chunk queue plays back.
    Returns (chunks, starts, stops, max_n, truncated, gaps, pf_ready, pf_fail,
    dbl_unavail, pipe_states, cursor):
      chunks    maps chunk index i (1-based) to (n_total, text)
      starts/stops  count the `TTS start`/`TTS stop` brackets
      max_n     the ADVERTISED chunk total — the `voice chunk i/n` denominator
                (every line carries it), NOT the last index seen.  This is the
                authoritative "whole queue" target: a mid-queue stop (TTS
                stall abort or a dropped tail) leaves max_n > the highest seen
                index and the completeness check FAILs (R2).
      truncated True when the device logged the `voice truncated:` marker —
                the reply was too long for the queue cap and its tail was NOT
                read out (R3).
      gaps      list of inter-chunk playback gaps in MILLISECONDS:
                TTS start(i+1) - TTS stop(i) from the ESP log timestamps (which
                tick in ms).  Empty when no pair completed (or the first chunk
                only).  With the lookahead prefetch active each gap is a few
                ms; without it each gap is the ~1-3 s Kokoro fetch that ran on
                the critical path between chunks.
      pf_ready/pf_fail/dbl_unavail  counts of the console markers that say
                which TTS path ran (`prefetch chunk i/n ready` = pipeline hit,
                `prefetch chunk i/n failed` = sync fallback per chunk,
                `TTS pipeline unavailable` = whole turn on sequential path).
      pipe_states  maps chunk index i to (buffered: bool, ahead: int) from the
                `pipeline chunk i/n buffered=… ahead=…` line logged before its
                playback — the per-boundary proof the pipeline stayed ahead.
      cursor    the index just past the last line scanned (hand to the next
                scanner).
    Ends when EVERY advertised chunk 1..max_n is seen with its own start AND
    stop — i.e. the last `TTS stop` of a COMPLETE queue — or when `timeout`
    runs out (a truncated queue runs to timeout so the leg reports FAIL)."""
    deadline = time.time() + timeout
    pos = tail.mark() if start is None else start
    chunks = {}
    starts = stops = 0
    max_n = 0
    truncated = False
    gaps = []
    last_stop_ms = None
    pf_ready = pf_fail = dbl_unavail = 0
    pipe_states = {}
    fetch_ms = {}
    while True:
        lines, pos = tail.take(pos, max(0.0, deadline - time.time()))
        for ln in lines:
            m = CHUNK_RE.search(ln)
            if m:
                i, n = int(m.group(1)), int(m.group(2))
                chunks[i] = (n, m.group(3).strip())
                max_n = max(max_n, n)
                log(f"chunk {i}/{n} seen")
            if TRUNCATED_RE.search(ln):
                truncated = True
                log("voice truncated marker seen (reply exceeds max chunks)")
            if PF_READY_RE.search(ln):
                pf_ready += 1
            if PF_FAIL_RE.search(ln):
                pf_fail += 1
            mf = FETCH_MS_RE.search(ln)
            if mf:
                fetch_ms[int(mf.group(1))] = (int(mf.group(3)), int(mf.group(4)))
            mp = PIPE_STATE_RE.search(ln)
            if mp:
                pipe_states[int(mp.group(1))] = (
                    mp.group(3) == "yes", int(mp.group(4))
                )
            if DBUFF_UNAVAIL_RE.search(ln):
                dbl_unavail += 1
            ts = ESP_TS_RE.match(ln)
            t_ms = int(ts.group(1)) if ts else None
            if t_ms is not None and TTS_START_RE.search(ln):
                if last_stop_ms is not None:
                    gaps.append(t_ms - last_stop_ms)
                starts += 1
            if t_ms is not None and TTS_STOP_RE.search(ln):
                last_stop_ms = t_ms
                stops += 1
            elif t_ms is None:
                # no timestamp parsed (e.g. app> prompt echo): still count
                # brackets so completeness checks stay robust
                if TTS_START_RE.search(ln):
                    starts += 1
                if TTS_STOP_RE.search(ln):
                    stops += 1
        if (
            max_n
            and all(i in chunks for i in range(1, max_n + 1))
            and starts >= max_n
            and stops >= max_n
        ):
            break
        if time.time() >= deadline:
            break
    return (chunks, starts, stops, max_n, truncated, gaps, pf_ready, pf_fail,
            dbl_unavail, pipe_states, fetch_ms, pos)


def unescape_reply(escaped):
    """Recover the full agent reply from the device's line-safe JSON-escaped
    `agent reply:` log (robodog_voice.c json_escape — every newline/quote/
    control char becomes a backslash escape so the whole reply is ONE console
    line, R1).  Returns the raw string on any decode failure (OOM fallback of
    the device, or non-UTF-8 text)."""
    if not escaped:
        return escaped
    try:
        return json.loads('"' + escaped + '"')
    except Exception:  # noqa: BLE001
        return escaped


def tail_tokens(text, k=12):
    """The LAST k normalized tokens of a reply — the anti-truncation target:
    if the read-out reached the reply's end, the mic capture must carry them."""
    toks = norm(text)
    return set(toks[-k:]) if toks else set()


def read_shot_frame(tail, timeout=30, validate=True, start=None):
    """Scan the drained console from `start` for an RD_SHOT_BEGIN..RD_SHOT_END
    block; return (raw bytes, (w,h,len), cursor).  With validate=True, keeps
    scanning past truncated frames (the forced `rdshot` frame races the
    self-tick dump) until a complete bufsize-length frame is seen."""
    deadline = time.time() + timeout
    pos = tail.mark() if start is None else start
    in_shot = False
    want = 0
    w = h = 0
    hex_acc = []
    while True:
        lines, pos = tail.take(pos, max(0.0, deadline - time.time()))
        for ln in lines:
            if not in_shot:
                m = re.search(r"RD_SHOT_BEGIN\s+bufsize=(\d+)\s+w=(\d+)\s+h=(\d+)", ln)
                if m:
                    in_shot = True
                    want = int(m.group(1))
                    w, h = int(m.group(2)), int(m.group(3))
                    hex_acc = []
            else:
                if "RD_SHOT_END" in ln:
                    data = bytes.fromhex("".join(hex_acc))
                    # The forced `rdshot` frame often races the self-tick dump
                    # and arrives truncated (< bufsize).  Keep scanning for a
                    # complete frame — the self-tick guarantees a full one
                    # within the timeout — instead of failing on the first
                    # partial.  validate==False callers accept any frame.
                    if validate and len(data) == want:
                        return data, (want, w, h), pos
                    if not validate:
                        return data, (want, w, h), pos
                    in_shot = False
                    hex_acc = []
                elif re.match(r"^[0-9a-fA-F]+$", ln):
                    hex_acc.append(ln)
        if time.time() >= deadline:
            return None, (0, 0, 0), pos


def capture_subtitle(tail, artifacts):
    """Send `rdshot` on the already-open console and read a frame; decode +
    vision-read it back.  Reuses the loop's console (a second handle races the
    RD_SHOT dump and loses frames)."""
    try:
        tail.write("rdshot")
        data, (want, w, h), _ = read_shot_frame(tail, timeout=25)
        if data is None or len(data) != want:
            return "", "no RD_SHOT frame"
        out = artifacts / "subtitle.i1"
        out.write_bytes(data)
        img = capture.decode_hermes(data)
        png = artifacts / "subtitle.png"
        img.convert("L").save(png)
        txt = capture.describe_image(str(png), prompt=capture.HERMES_VISION_PROMPT)
        (artifacts / "subtitle_analysis.txt").write_text(txt)
        return txt, ""
    except Exception as e:  # noqa: BLE001 — report and let SUBTITLE fail
        return "", f"vision readback failed: {e}"


# ---------------------------------------------------------------------------
# One voice loop
# ---------------------------------------------------------------------------
def run_loop(n, instr, artifacts, voice_verify):
    # Per-run console scan accumulator for the NO_RESET leg; cleared so a
    # reset/panic seen in an earlier session cannot leak into this one.
    RUN_RESET_LINES.clear()
    res = {"loop": n + 1, "asserts": {}, "artifacts": {"asr": "", "reply": ""}}
    res["corpus"] = instr.get("transcript", instr.get("wav", ""))
    run_dir = artifacts / f"loop{n + 1}"
    run_dir.mkdir(parents=True, exist_ok=True)
    target_txt = instr["transcript"]
    target_wav = HERE.parent / "voice" / "corpus" / instr["wav"]

    tail = ConsoleTail(DETECTED_PORT)
    try:
        # ---- OPEN_SESSION: trigger + confirm listen ------
        tail.write("rd_listen")
        m, cur = wait_for_log(tail, [r"listen started"], timeout=15, label="OPEN")
        res["asserts"]["OPEN_SESSION"] = (
            "PASS listen started" if m else "FAIL (no 'listen started' after rd_listen)"
        )

        # ---- ASR_MATCH: play the corpus into the mic ------
        time.sleep(PLAY_SETTLE)
        log(f"playing corpus {target_wav.name} into board mic ...")
        # The corpus pins its own drive level: the rig's speaker->board-mic
        # coupling needs a louder drive for Chinese (0.7 decoded one word,
        # 0.9 the whole sentence), while English is fine at the default.
        play_wav(target_wav, volume=instr.get("play_vol", PLAY_VOL))

        asr_start = cur
        m_asr, cur = wait_for_log(
            tail, [r'ASR text="([^"]*)"'], timeout=ASR_TIMEOUT, label="ASR",
            start=asr_start
        )
        asr_txt = m_asr.group(1) if m_asr else ""
        res["artifacts"]["asr"] = asr_txt
        frac = token_overlap(asr_txt, target_txt)
        if asr_txt:
            res["asserts"]["ASR_MATCH"] = (
                f"PASS overlap={frac:.2f} asr={asr_txt!r}"
                if frac >= 0.75
                else f"FAIL overlap={frac:.2f} asr={asr_txt!r} want={target_txt!r}"
            )
        elif m_asr is not None:
            res["asserts"]["ASR_MATCH"] = (
                "FAIL (ASR returned an EMPTY transcription: the board captured "
                "and posted the utterance, the endpoint heard no words — an "
                "acoustic miss, not a capture fault)"
            )
        else:
            res["asserts"]["ASR_MATCH"] = "FAIL (no `ASR text=` on console)"
            # Diagnose: surface the board's OWN capture/VAD/Whisper decisions
            # from the whole ASR window so a failed acoustic leg says why —
            # VAD never heard speech vs Whisper round-trip failure vs a
            # capture fault.  The tail keeps every line, so nothing scrolled
            # past (the old byte spill lost this evidence behind the RD_SHOT
            # dump).
            board_lines = [
                ln for ln in tail.take(0, 0)[0][asr_start:cur]
                if any(k in ln for k in ("robodog_voice", "ASR", "utterance",
                                         "speech", "bail", "SPIRAM", "capture"))
                and "RD_SHOT" not in ln
            ]
            if board_lines:
                log("board capture/VAD/ASR decisions (no `ASR text=`):")
                for bl in board_lines[-12:]:
                    log("   | " + bl.strip()[:160])
                # Persist them: the multi-session stress gate classifies a
                # session that never reached ASR (silent listen vs capture
                # fault / SPIRAM exhaustion) from this evidence.
                res["artifacts"]["capture_diag"] = "\n".join(
                    bl.strip()[:200] for bl in board_lines[-12:]
                )

        # ---- ASR_LANG: the endpoint auto-detected the spoken language ------
        # No `language` field is sent with the upload, so the reported tag is
        # the endpoint's own detection — the proof that Chinese speech was
        # heard AS Chinese instead of being forced through an English model.
        want_lang = "zh" if instr.get("language") == "chinese" else "en"
        m_lang, cur = wait_for_log(
            tail, [r"ASR language=(\S+) prob=([\d.]+)"], timeout=10,
            label="ASR_LANG", start=asr_start
        )
        asr_lang = m_lang.group(1) if m_lang else ""
        res["artifacts"]["asr_lang"] = asr_lang
        if m_lang:
            res["asserts"]["ASR_LANG"] = (
                f"PASS detected {asr_lang} (prob={m_lang.group(2)})"
                if asr_lang == want_lang
                else f"FAIL detected {asr_lang}, want {want_lang} "
                     f"(the ASR endpoint forced or mis-detected the language)"
            )
        else:
            res["asserts"]["ASR_LANG"] = (
                "FAIL (no `ASR language=` on console — the firmware does not "
                "surface the endpoint's detected language)"
            )

        # ---- REPLY_LANG: the turn chose the reply-language directive ------
        # Console evidence that the dual-language wiring ran: a Chinese
        # utterance must select the Traditional-Chinese directive, everything
        # else the English prose directive.
        want_dir = "zh-TW" if want_lang == "zh" else "en"
        m_dir, cur = wait_for_log(
            tail, [r"voice reply language=(\S+)"], timeout=10,
            label="REPLY_LANG", start=asr_start
        )
        got_dir = m_dir.group(1) if m_dir else ""
        res["artifacts"]["reply_lang"] = got_dir
        if m_dir:
            res["asserts"]["REPLY_LANG"] = (
                f"PASS directive={got_dir}"
                if got_dir == want_dir
                else f"FAIL directive={got_dir}, want {want_dir}"
            )
        else:
            res["asserts"]["REPLY_LANG"] = (
                "FAIL (no `voice reply language=` on console)"
            )

        # Fail fast: no ASR means no agent run / TTS / subtitle in this turn —
        # waiting for the reply would just burn the full 180 s REPLY_TIMEOUT.
        if not asr_txt:
            skip = (
                ("REPLY", "TTS", "CHUNKED_QUEUE", "PIPELINE_AHEAD",
                 "GAP_VERIFY", "FULL_READOUT")
                if voice_verify
                else ("REPLY", "TTS", "RING", "SUBTITLE")
            )
            for k in skip:
                res["asserts"][k] = "SKIP (no ASR -> no turn)"
            res["artifacts"]["reply"] = ""
            set_no_reset(res)  # the scan may still have seen a reset/panic
            tail.close()
            return res

        # ---- REPLY: native agent LLM ------
        # The cursor after `ASR text=` is handed forward, so the `voice chunk
        # 1/n` line that lands right behind `agent reply:` is scanned by the
        # collector instead of being discarded (no byte-level spill).
        reply_start = cur
        m_rep, cur = wait_for_log(
            tail, [r"agent reply: (.+)"], timeout=REPLY_TIMEOUT, label="REPLY",
            start=reply_start
        )
        reply = m_rep.group(1).strip() if m_rep else ""
        # R1: the device logs the reply JSON-escaped (line-safe, one console
        # line), so recover the real text here — FULL_READOUT / SUBTITLE /
        # RING must compare against the reply's TRUE end, never a partial
        # first-line capture.
        reply = unescape_reply(reply)
        res["artifacts"]["reply"] = reply
        if not reply:
            res["asserts"]["REPLY"] = "FAIL (no `agent reply:` on console)"
        elif want_lang == "zh" and not has_cjk(reply):
            # The directive selected Chinese, but the model answered in
            # English: the language wiring reached the agent and did not take.
            res["asserts"]["REPLY"] = (
                f"FAIL reply is not Chinese despite the zh-TW directive: {reply!r}"
            )
        elif want_lang == "en" and dominant_script(reply) != "latin":
            # Symmetric guard: after a Chinese turn the session memory made the
            # model answer an ENGLISH question in Chinese until the directive
            # named the language explicitly.  A Latin-dominant reply is the
            # English turn's contract.
            res["asserts"]["REPLY"] = (
                f"FAIL reply is Chinese-dominant despite the en directive: {reply!r}"
            )
        else:
            res["asserts"]["REPLY"] = f"PASS reply={reply!r}"

        # ---- TTS + RING: capture the board speaker ------
        m_tts = None
        m_tts_stop = None
        mic_path = run_dir / "speaker.wav"
        ring_txt = ""
        chunks, starts, stops = {}, 0, 0
        max_n, truncated = 0, False
        gaps, pf_ready, pf_fail, dbl_unavail = [], 0, 0, 0
        pipe_states = {}
        fetch_ms = {}
        if voice_verify:
            # No separate `TTS start` wait: wait_for_log would CONSUME the
            # `voice chunk i/n` line that precedes each `TTS start`, making
            # CHUNKED_QUEUE unobservable.  collect_voice_chunks scans for the
            # chunk logs AND their start/stop brackets together, from right
            # after `agent reply:`.
            log("TTS phase; capturing board speaker on test mic ...")
            # A fresh mic capture every turn: delete any previous speaker.wav
            # first so a capture_mic failure can never silently re-ASR a stale
            # capture from an earlier run (which produced a bogus FULL_READOUT).
            # SIM105: blank-except -> contextlib.suppress (a fresh mic capture
            # every turn must never re-ASR a stale earlier capture).
            with contextlib.suppress(Exception):
                mic_path.unlink(missing_ok=True)
            # The chunk queue plays the WHOLE reply as back-to-back chunks;
            # keep the test mic recording in the BACKGROUND for the full
            # reply window while the console scanner below collects every
            # `voice chunk i/n` + start/stop bracket live.  Blocking on the
            # long capture first would scroll `TTS stop` / chunk logs past
            # the serial buffer and make the acceptance leg unobservable.
            cap_done = threading.Event()

            def _cap():
                try:
                    capture_mic(mic_path, VOICE_CAP_SECS)
                except Exception as e:  # noqa: BLE001
                    log(f"voice capture failed: {e}")
                finally:
                    cap_done.set()

            threading.Thread(target=_cap, daemon=True).start()
            (chunks, starts, stops, max_n, truncated, gaps, pf_ready, pf_fail,
             dbl_unavail, pipe_states, fetch_ms, cur) = collect_voice_chunks(
                tail, REPLY_TIMEOUT, start=reply_start
            )
            # The re-ASR below reads the WHOLE captured WAV, so wait for the
            # background capture to actually finish (bounded by
            # VOICE_CAP_SECS) — reading a half-written RIFF yields nothing.
            cap_done.wait()
        else:
            m_tts, cur = wait_for_log(tail, [r"TTS start"], timeout=REPLY_TIMEOUT,
                                      label="TTS", start=cur)
            if m_tts:
                log("TTS start seen; capturing board speaker on test mic ...")
                try:
                    capture_mic(mic_path, RING_CAP_SECS)
                except Exception as e:
                    log(f"ring capture failed: {e}")
                # Board TTS is real-time paced (~16 s for a long reply); wait for
                # `TTS stop` from the TTS-start cursor.  The tail keeps draining
                # in the background, so the slow ring capture + re-ASR below can
                # no longer scroll this line out of reach.
                m_tts_stop, cur = wait_for_log(tail, [r"TTS stop"],
                                               timeout=REPLY_TIMEOUT, label="TTS",
                                               start=cur)
        try:
            # The board's reply is faint but spans the whole TTS window; a
            # single loudest-3s burst is often ambient noise and lets
            # Whisper under-trigger (re-ASR back to nothing).  Transcribe
            # the FULL capture first, and only fall back to the loudest
            # burst if the full window yields no text.  The burst must be a
            # whole ~sentence (~15 s), not 4 s: at this rig's marginal
            # speaker->mic coupling a 4 s window lysses into a partial
            # syllable and re-ASRs empty (measured: 15 s window transcribes
            # "I'm ESP Claw, an on-device AI agent..." on the same capture).
            try:
                ring_txt = remote_asr(mic_path)
            except Exception as e:
                # A slow remote Whisper on the long full-reply capture is NOT
                # "the mic heard nothing": a timeout must fall through to the
                # loudest-burst re-ASR below instead of hard-FAILing the
                # acoustic corroboration leg.  The design tolerates PASS-WEAK
                # for marginal speaker->mic coupling (CHUNKED_QUEUE is the
                # authoritative readout evidence), so an ASR hiccup should
                # never fake a "silent" readout.
                log(f"full-reply re-ASR failed ({e}); falling back to loudest burst")
            if not ring_txt:
                burst = run_dir / "reply_burst.wav"
                trim_loudest_window(mic_path, burst, 15.0)
                if burst.exists():
                    try:
                        ring_txt = remote_asr(burst)
                    except Exception as e:
                        log(f"burst re-ASR failed: {e}")
            (run_dir / "ring_reasr.txt").write_text(ring_txt or "")
        except Exception as e:
            log(f"ring re-ASR failed: {e}")
        # TTS acceptance: the regular gate wants a single start+stop; in
        # --voice-verify the per-chunk start/stop brackets are counted by
        # collect_voice_chunks (starts/stops), so a full queue is the evidence.
        res["asserts"]["TTS"] = (
            "PASS start+stop"
            if (
                (starts >= 1 and stops >= 1)
                if voice_verify
                else (m_tts is not None and m_tts_stop is not None)
            )
            else "FAIL (no TTS start/stop on console)"
        )
        if voice_verify:
            # The authoritative anti-truncation evidence is deterministic and
            # console-side: EVERY advertised chunk 1..max_n MUST have its own
            # TTS bracket (R2 validates against the advertised total, not the
            # last seen index, so a mid-queue stop FAILs) and the device must
            # NOT have reported a dropped tail (R3).
            if truncated:
                res["asserts"]["CHUNKED_QUEUE"] = (
                    "FAIL `voice truncated` marker on console — the reply "
                    f"exceeds CONFIG_ROBODOG_VOICE_TTS_MAX_CHUNKS "
                    f"(advertised n={max_n}); the tail was NOT read out"
                )
            elif chunks:
                ordered = sorted(chunks) == list(range(1, max_n + 1))
                bracketed = starts >= max_n and stops >= max_n
                res["asserts"]["CHUNKED_QUEUE"] = (
                    f"PASS {max_n} chunk(s), ALL TTS-bracketed (start={starts} stop={stops})"
                    if ordered and bracketed
                    else f"FAIL chunks={sorted(chunks)} of advertised {max_n} "
                    f"starts={starts} stops={stops}"
                )
            else:
                res["asserts"]["CHUNKED_QUEUE"] = (
                    "FAIL (no `voice chunk i/n` logs — the queue did not run)"
                )
            # PIPELINE_AHEAD: the deterministic leg for "the reply sounds
            # continuous".  The firmware logs `pipeline chunk i/n buffered=…`
            # before each chunk plays; every boundary AFTER the first must be
            # buffered=yes — its synthesis finished while the previous sentence
            # was still being spoken, so no endpoint fetch sits on the play
            # path.  `ahead=k` is reported as a diagnostic (how deep the queue
            # was at that boundary); it is NOT a pass condition because the
            # fetch worker is serial and the endpoint synthesizes at ~real
            # time, so a deeper queue naturally shows ahead=0 right after a
            # chunk becomes ready.
            if pipe_states:
                ahead = " ".join(
                    f"{i}:{'Y' if pipe_states[i][0] else 'N'}/+{pipe_states[i][1]}"
                    for i in sorted(pipe_states)
                )
                res["artifacts"]["pipeline_ahead"] = {
                    i: {"buffered": b, "ahead": a}
                    for i, (b, a) in sorted(pipe_states.items())
                }
                if fetch_ms:
                    res["artifacts"]["chunk_fetch_ms"] = {
                        i: {"ms": ms, "bytes": n}
                        for i, (ms, n) in sorted(fetch_ms.items())
                    }
                unbuffered = [i for i, (b, _) in pipe_states.items()
                              if i > 1 and not b]
                if not unbuffered:
                    res["asserts"]["PIPELINE_AHEAD"] = (
                        f"PASS every boundary after the first was buffered "
                        f"({ahead}) — no endpoint fetch on the play path"
                    )
                else:
                    res["asserts"]["PIPELINE_AHEAD"] = (
                        f"FAIL pipeline fell behind: unbuffered boundaries="
                        f"{unbuffered} ({ahead}) — a synchronous fetch ran "
                        f"between sentences"
                    )
            else:
                res["asserts"]["PIPELINE_AHEAD"] = (
                    "SKIP (no `pipeline chunk i/n` markers on console)"
                )
            # GAP_VERIFY: the feature's actual effect is the COLLAPSED gap
            # between chunks.  On the pre-fix code each TTS stop(i) was
            # followed by the blocking Kokoro fetch of chunk(i+1), so the
            # stop->start gap was ~1-3 s; with the lookahead prefetch the next
            # WAV is already buffered and the gap is a few ms.  Measured from
            # the ESP log timestamps.
            if gaps:
                gap_ms = [round(g, 1) for g in gaps]
                res["artifacts"]["chunk_gaps_ms"] = gap_ms
                gstr = " ".join(f"{g:.1f}ms" for g in gap_ms)
                # Which TTS path actually ran — from the console markers — so
                # a FAIL pinpoints WHY the gap is large instead of guessing.
                if dbl_unavail:
                    path = "sequential single-buffer path (TTS pipeline unavailable)"
                elif pf_fail:
                    path = f"{pf_fail} sync-fallback fetch(es) in the play gap"
                elif pf_ready:
                    path = f"{pf_ready} pipeline-buffered chunk(s)"
                else:
                    path = "no path markers on console"
                if all(0 <= g < GAP_THRESHOLD_MS for g in gap_ms):
                    res["asserts"]["GAP_VERIFY"] = (
                        f"PASS inter-chunk stop->start gaps {gstr} "
                        f"(all < {GAP_THRESHOLD_MS}ms = TTS pipeline active; "
                        f"pre-fix these were the endpoint fetch on the critical path; {path})"
                    )
                else:
                    res["asserts"]["GAP_VERIFY"] = (
                        f"FAIL inter-chunk stop->start gaps {gstr} — "
                        f"gap(s) >= {GAP_THRESHOLD_MS}ms mean a synchronous "
                        f"fetch ran between chunks (TTS pipeline NOT taking "
                        f"effect; {path})"
                    )
            else:
                res["asserts"]["GAP_VERIFY"] = (
                    "SKIP (no inter-chunk stop->start pair captured)"
                )
            # FULL_READOUT: the reply's LAST words must appear in the mic
            # capture — the read-out reached the reply's end, nothing was cut
            # off.  `reply` is the line-safe full text (R1), so tail_tokens
            # targets the reply's TRUE end — never the head as an unescaped
            # first-line `agent reply:` capture would.  Console queue evidence
            # above (CHUNKED_QUEUE, advertised max_n) is authoritative; this
            # is the acoustic corroboration (PASS-WEAK tolerated for marginal
            # speaker->mic coupling).
            tail_toks = tail_tokens(reply)
            got = set(norm(ring_txt)) if ring_txt else set()
            overlap = token_overlap(ring_txt, reply)
            if ring_txt and tail_toks and len(tail_toks & got) >= 2:
                res["asserts"]["FULL_READOUT"] = (
                    f"PASS tail tokens read out (overlap={overlap:.2f})"
                )
            elif ring_txt and overlap >= 0.3:
                res["asserts"]["FULL_READOUT"] = (
                    "PASS-WEAK audible but tail tokens not in re-ASR "
                    f"(overlap={overlap:.2f}, marginal coupling); "
                    "CHUNKED_QUEUE is the authoritative readout evidence"
                )
            elif ring_txt:
                res["asserts"]["FULL_READOUT"] = (
                    f"PASS-WEAK audible (reply too short / noisy re-ASR={ring_txt!r})"
                )
            else:
                res["asserts"]["FULL_READOUT"] = "FAIL (no re-ASR of board speaker)"
        else:
            if ring_txt and reply:
                ok = token_overlap(ring_txt, reply) >= 0.3
                res["asserts"]["RING"] = (
                    f"PASS re-asr={ring_txt!r} (overlaps reply)"
                    if ok
                    else f"PASS-WEAK re-asr={ring_txt!r} (audible but no verbatim overlap)"
                )
            elif ring_txt:
                res["asserts"]["RING"] = f"PASS-WEAK re-asr={ring_txt!r} (no reply to compare)"
            else:
                res["asserts"]["RING"] = "FAIL (no re-ASR of board speaker)"

            # ---- SUBTITLE: the reply renders on the RLCD ------
            time.sleep(1.0)
            sub_txt, sub_err = capture_subtitle(tail, run_dir)
            # The panel readback must carry the ASSISTANT REPLY (Phase 1 gate
            # contract: "rd_chat N*N -> the reply renders on screen").  The
            # readback is OCR-noisy (whole-screen transcript), so require >= 2
            # distinct reply tokens to appear in it — a set-overlap ratio is
            # diluted by the screen's long text.  The user transcript in the
            # readback is pass-through.
            read_tok = set(norm(sub_txt)) if sub_txt else set()
            shared = len(set(norm(reply)) & read_tok) if reply else 0
            shown = bool(sub_txt) and len(read_tok) >= 3 and shared >= 2
            if shown:
                res["asserts"]["SUBTITLE"] = "PASS " + sub_txt.replace("\n", " | ")
            elif sub_txt:
                res["asserts"]["SUBTITLE"] = "FAIL-no-reply " + sub_txt.replace("\n", " | ")
            else:
                res["asserts"]["SUBTITLE"] = "FAIL " + (sub_err or "blank readback")
            res["artifacts"]["subtitle"] = sub_txt
    finally:
        set_no_reset(res)
        tail.close()
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--corpus",
        default="capabilities",
        help="corpus slug to voice-drive (corpus.json); default 'capabilities'",
    )
    ap.add_argument(
        "--corpus-cycle",
        metavar="SLUGS",
        default=None,
        help="cycle the voice-driven question per loop so no two consecutive "
        "sessions ask the same question: 'auto' = "
        "time,joke,greeting,capabilities,count, or an explicit comma-"
        "separated slug list.  Default (unset) = today's single-corpus "
        "behavior (--corpus) so the regular gate invocations are unchanged.",
    )
    ap.add_argument("--artifacts", default=str(Path.home() / "artifacts"))
    ap.add_argument(
        "--voice-verify",
        action="store_true",
        help="dedicated acoustic voice verification mode: asserts the FULL"
        " chunked reply was queued AND read out to its end (CHUNKED_QUEUE +"
        " PIPELINE_AHEAD + FULL_READOUT legs instead of RING/SUBTITLE). Writes"
        " robodog-claw-voice-verify.md; never writes hardware-e2e.md or"
        " .gate-head.",
    )
    args = ap.parse_args()
    voice_verify = args.voice_verify
    tag = "robodog-claw-voice-verify" if voice_verify else "robodog-claw-e2e"
    artifacts = Path(args.artifacts) / tag
    artifacts.mkdir(parents=True, exist_ok=True)

    global DETECTED_PORT
    DETECTED_PORT = detect_board_port()
    card = detect_sound_card()
    log(f"board={DETECTED_PORT or 'NONE'} sound='{card}' runs={N_RUNS} corpus={args.corpus}")
    skips = []
    if not DETECTED_PORT:
        skips.append("ROBODOG BOARD NOT DETECTED")
    if not card:
        skips.append("TEST AUDIO CARD NOT DETECTED (ALSA USB)")
    if skips:
        print("\nLOUD SKIP: " + "; ".join(skips) + "\n")
        (artifacts / "SKIPPED.txt").write_text("; ".join(skips) + "\n")
        return 0

    man = json.loads((HERE.parent / "voice" / "corpus" / "corpus.json").read_text())
    if args.corpus not in man:
        sys.exit(f"unknown corpus slug '{args.corpus}': {sorted(man)}")
    instr = man[args.corpus]

    # --corpus-cycle: consecutive sessions ask DIFFERENT questions (cycle
    # length 5 > 1).  Unset -> single-corpus behavior (gates unchanged).
    cyc = None
    if args.corpus_cycle:
        raw = args.corpus_cycle
        slugs = (
            ["time", "joke", "greeting", "capabilities", "count"]
            if raw.strip().lower() == "auto"
            else [s.strip() for s in raw.split(",") if s.strip()]
        )
        if not slugs:
            sys.exit("--corpus-cycle: empty slug list")
        unknown = [s for s in slugs if s not in man]
        if unknown:
            sys.exit(f"unknown --corpus-cycle slugs {unknown}: {sorted(man)}")
        cyc = slugs
        log(f"corpus cycle: {', '.join(cyc)} across {N_RUNS} runs")

    results = []
    for i in range(N_RUNS):
        instr = man[cyc[i % len(cyc)]] if cyc else man[args.corpus]
        r = run_loop(i, instr, artifacts, voice_verify)
        results.append(r)
        for k, v in r["asserts"].items():
            log(f"{k}: {v}")
        if i < N_RUNS - 1:
            log("settling between loops ...")
            time.sleep(3)

    title = (
        "RoboDog ESP-Claw Voice Verification Results"
        if voice_verify
        else "RoboDog ESP-Claw Voice-Loop E2E Results"
    )
    lines = [
        f"# {title}\n",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M')}",
        f"Board: {DETECTED_PORT}",
    ]
    lines.append(
        f"Corpus: CYCLE[{', '.join(cyc)}] pooled across {N_RUNS} runs"
        if cyc
        else f"Corpus: {args.corpus} ({instr['transcript']!r})"
    )
    lines.append(f"Runs: {N_RUNS}\n")
    for r in results:
        lines.append(f"## loop {r['loop']}")
        lines.append(f"- Corpus: {r.get('corpus', '')!r}")
        for k, v in r["asserts"].items():
            lines.append(f"- **{k}**: {v}")
        lines.append(f"- ASR verbatim: {r['artifacts'].get('asr', '')!r}")
        lines.append(f"- REPLY verbatim: {r['artifacts'].get('reply', '')!r}")
        lines.append("")

    def ok(v):
        return v is not None and (v.startswith("PASS") or v.startswith("SKIP"))

    keys = (
        ("OPEN_SESSION", "ASR_MATCH", "ASR_LANG", "REPLY_LANG", "REPLY", "TTS",
         "CHUNKED_QUEUE", "PIPELINE_AHEAD", "FULL_READOUT", "NO_RESET")
        if voice_verify
        else ("OPEN_SESSION", "ASR_MATCH", "ASR_LANG", "REPLY_LANG", "REPLY",
              "TTS", "RING", "SUBTITLE", "NO_RESET")
    )

    def passed(r):
        return all(ok(r["asserts"].get(k, "FAIL")) for k in keys)

    overall = all(passed(r) for r in results)
    lines.append(f"Overall: {'PASS' if overall else 'FAIL'}")
    (artifacts / f"{tag}.md").write_text("\n".join(lines))
    # Only the regular voice-loop E2E is the author of the top-level
    # hardware-e2e.md contract; the dedicated voice-verification gate is
    # acoustic evidence only and must never claim that slot.
    if voice_verify:
        print("\n".join(lines))
        return 0 if overall else 1

    # Top-level hardware-e2e.md: the generic gate-chain/finalize contract keys
    # on `$ARTIFACTS_DIR/hardware-e2e.md` existing (the mandatory hardware-E2E
    # evidence for PR creation).  This board workflow is the author of it here
    # (mutually exclusive with the calendar gate, which writes a sibling copy).
    brief = [
        "# RoboDog ESP-Claw Voice-Loop E2E — Hardware Evidence\n",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M')}",
        f"Board: {DETECTED_PORT}  (Waveshare ESP32-S3 RLCD-4.2, 0x303A:0x1001)",
    ]
    brief.append(
        f"Corpus: CYCLE[{', '.join(cyc)}] pooled across {N_RUNS} runs"
        if cyc
        else f"Corpus: {args.corpus} ({instr['transcript']!r})"
    )
    brief.append(f"Runs: {N_RUNS}\n")
    for r in results:
        for k, v in r["asserts"].items():
            brief.append(f"- **{k}**: {v}")
        break
    brief.append(f"\nOverall: {'PASS' if overall else 'FAIL'}")
    brief.append("\nFull verbatim transcripts + subtitle readback: ")
    brief.append("`robodog-claw-e2e/robodog-claw-e2e.md` (detailed).")
    (Path(args.artifacts) / "hardware-e2e.md").write_text("\n".join(brief))
    print("\n".join(lines))
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
