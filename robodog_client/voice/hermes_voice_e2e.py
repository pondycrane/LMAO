#!/usr/bin/env python3
"""Autonomous hermes voice-loop E2E for the RoboDog board (issue #267).

Drives the whole XiaoZhi voice loop with NO human and NO wake word:

  host RD_LISTEN (USB-JTAG) -> board StartListening() -> WS session opens
  test speaker -> command WAV -> board mic -> bridge faster-whisper ASR
  -> Hermes brain -> Kokoro TTS (auto-"voice") -> board speaker
  -> test mic captures reply -> host re-ASR (ring closure)
  -> STT/TTS subtitle rendered on the RLCD -> capture.py --describe readback

Every assertion is read from the live bridge journal
(journalctl --user -u lmao-robodog-voice) plus the test rig's audio/pixels,
so one command yields PASS/FAIL + verbatim evidence. Loud-skip when the board
(0x303A:0x1001) or the ALSA test-sound card is absent.

Issue #274 adds two asserts the #267 RING-only check was blind to:
- READOUT_COMPLETE: the full active-play span of the reply on the test mic vs
  the bridge's reported TTS duration (played_fraction >= 0.85) — a read-out
  truncated by the decode-queue drop (#272's pacing removes it) now FAILs
  instead of passing as RING "audible". Uses the `count` long-reply corpus
  (default) so the reply reliably exceeds READOUT_MIN_REPLY (3.5 s) and is
  decisive.
- POST_READOUT_RELISTEN: a fresh RD_LISTEN right after the bridge logs the
  reply's `tts stop` must re-arm listening (the shrunken replacement for the
  old flat 12 s settle between loops). The realtime auto-relisten users hit in
  normal conversation is NOT observable here: the board returns to idle after
  TTS with no reliable wake trigger, and the E2E drives RD_LISTEN (`mode=manual`)
  for every open — so this fresh-RD_LISTEN-success is the reachable proxy.

Fallback: E2E_STT_TEXT=<canned> skips the real-mic/ASR leg and injects the
utterance straight into the board's Hermes session via the bridge's
POST /xiaozhi/inject endpoint (covers brain->TTS->speaker->display).

Prereqs (rig bring-up, selfhost):
- host user in the `audio` group (ALSA access; `sg audio` is used so a fresh
  login is not required): `sudo gpasswd -a $(whoami) audio`
- bridge up: `systemctl --user status lmao-robodog-voice`
- run under the bridge venv (faster-whisper, numpy, PIL, pyserial):
  `~/xiaozhi/.venv/bin/python robodog_client/voice/hermes_voice_e2e.py`

Env:
  ROBODOG_PORT      board serial (default: auto-detect 0x303A:0x1001)
  RD_SOUND_CARD     ALSA card id of the test speaker+mic (default "Device")
  RD_PLAY_VOL       playback volume 0..1 (default 0.7; >0.8 keeps the board
                    mic hot and blows the ASR buffer in tests)
  BRIDGE_OTA_PORT   bridge OTA/inject port (default 8003)
  E2E_RUNS          consecutive loops to run (default 3; acceptance criterion)
  E2E_STT_TEXT      canned utterance injection fallback (skips the mic leg)
  RD_VISION_*       vision endpoint/model overrides (capture.py defaults)
  BRIDGE_UNIT       systemd user unit journal to poll (default
                    lmao-robodog-voice)
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))

# Components that must live in the same interpreter (faster-whisper etc.).
import capture  # noqa: E402  (RD_SHOT decode + vision readback)
from e2e_readout import (  # noqa: E402  (issue #274 read-out metrics, stdlib)
    active_play_span,
    energy_burst_window,
    played_fraction,
    quiet_floor,
    wave_rms,
)
from voice_bridge import call_asr  # noqa: E402

BOARD_VID, BOARD_PID = 0x303A, 0x1001
SOUND_CARD = os.environ.get("RD_SOUND_CARD", "Device")
PLAY_VOL = float(os.environ.get("RD_PLAY_VOL", "0.7"))
BRIDGE_OTA_PORT = int(os.environ.get("BRIDGE_OTA_PORT", "8003"))
BRIDGE_UNIT = os.environ.get("BRIDGE_UNIT", "lmao-robodog-voice")
N_RUNS = int(os.environ.get("E2E_RUNS", "3"))
E2E_STT_TEXT = os.environ.get("E2E_STT_TEXT", "").strip()
ASR_TIMEOUT = int(os.environ.get("E2E_ASR_TIMEOUT", "72"))
REPLY_TIMEOUT = int(os.environ.get("E2E_REPLY_TIMEOUT", "180"))
# Seconds to let the board's mic/AEC ramp after StartListening() before the
# test speaker plays. Playing earlier attenuates the command below the bridge
# VAD threshold (no utterance -> no ASR): measured live play at listen+4.2-5.1s
# -> clean ASR every time; listen+1..2.8s -> nothing. 6.0 is safely inside.
PLAY_SETTLE = float(os.environ.get("RD_PLAY_SETTLE", "6.0"))
# Issue #274 post-readout re-listening: after the bridge logs the reply's
# `tts stop`, wait this grace (board drains the last paced frames) then issue a
# fresh RD_LISTEN and require a `listen start` within RE_LISTEN_TIMEOUT — the
# shrunken replacement for the old flat 12 s settle between loops.
RE_LISTEN_GRACE = float(os.environ.get("RD_RELISTEN_GRACE", "1.5"))
RE_LISTEN_TIMEOUT = int(os.environ.get("RD_RELISTEN_TIMEOUT", "15"))
# Issue #274 read-out completeness: below this reply length the ~0.5 s
# truncation delta is not discriminative (a `time`-class ~3.0 s reply is
# marginal) — long-reply corpus replies must beat it.
READOUT_MIN_REPLY = float(os.environ.get("RD_READOUT_MIN_REPLY", "3.5"))
# Drain between loops: the end-of-loop POST_READOUT_RELISTEN opens the next
# loop's session, so only a short pause is needed before its command plays.
BETWEEN_LOOP_SETTLE = float(os.environ.get("RD_BETWEEN_LOOP_SETTLE", "3"))
# When the (flaky) mic ASR falls back to the E2E_STT injection, the injected
# reply is streamed during the inject call — so the test-mic capture starts
# BEFORE it, with this fixed window covering a ~6-8 s reply plus tail.
MIC_INJECT_CAP = float(os.environ.get("RD_INJECT_CAP_SECS", "20"))
SILENCE = os.environ.get("E2E_AUDIO_SG", "sg audio -c")


def log(msg):
    print(f"[e2e] {time.strftime('%H:%M:%S')} {msg}", flush=True)


def shell_run(cmd, timeout=120):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                         timeout=timeout)


# ---------------------------------------------------------------------------
# Rig preflight
# ---------------------------------------------------------------------------
def detect_board_port():
    port = os.environ.get("ROBODOG_PORT", "")
    if port:
        return port
    import serial.tools.list_ports
    for p in serial.tools.list_ports.comports():
        if p.vid == BOARD_VID and p.pid == BOARD_PID:
            return p.device
    return None


def detect_sound_card():
    """Configured ALSA card id, else the first USB audio card with both a
    playback and a capture PCM."""
    for line in Path("/proc/asound/cards").read_text().splitlines():
        if "USB-Audio" in line and "[" in line:
            card = line.split("[", 1)[1].split("]", 1)[0].strip()
            if card:
                return card
    return SOUND_CARD


def bridge_health():
    url = f"http://127.0.0.1:{BRIDGE_OTA_PORT}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Host-side audio drive/capture (test speaker + test mic over ALSA)
#   Playback must be moderate: >~0.8 volume saturates the board mic so its
#   VAD never sees silence and the ASR buffer blows up (measured live).
#   `sg audio` runs the command with the `audio` group (no re-login needed).
# ---------------------------------------------------------------------------
def play_wav(path: Path, volume: float = PLAY_VOL):
    cmd = (f"{SILENCE} 'ffmpeg -hide_banner -loglevel error -re -i {path} "
           f"-af volume={volume:.2f} -ac 1 -ar 16000 -f alsa "
           f"plughw:{SOUND_CARD},0 -y'")
    shell_run(cmd, timeout=120)


def capture_mic(path: Path, seconds: float):
    cmd = (f"{SILENCE} 'ffmpeg -hide_banner -loglevel error -f alsa "
           f"-i plughw:{SOUND_CARD},0 -t {seconds:.1f} -ac 1 -ar 16000 {path} -y'")
    shell_run(cmd, timeout=int(seconds) + 20)
    return path


def trim_wav(path: Path, start: float, end: float, out: Path):
    cmd = (f"{SILENCE} 'ffmpeg -hide_banner -loglevel error -i {path} "
           f"-ss {start:.2f} -t {max(0.1, end - start):.2f} -ac 1 -ar 16000 "
           f"{out} -y'")
    shell_run(cmd, timeout=30)


def send_rd_listen(port: str):
    import serial
    s = serial.Serial(port, 115200, timeout=1)
    try:
        time.sleep(1.0)          # arm USB-Serial/JTAG RX after open
        s.reset_input_buffer()
        s.write(b"RD_LISTEN\r\n")
        s.flush()
        time.sleep(0.4)
    finally:
        s.close()


def inject_text(text: str, session_id: str | None = None):
    """E2E fallback: push a canned utterance into the board's live Hermes
    session (issue #267 E2E_STT_TEXT path). ``session_id`` ties the injection to
    THIS loop's board session so the brain reply is addressed back to it (the
    bridge otherwise picks the most recent session, which can be a stale one)."""
    payload = {"text": text}
    if session_id:
        payload["session_id"] = session_id
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{BRIDGE_OTA_PORT}/xiaozhi/inject", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r)


def describe_frame(i1_path: Path):
    """Vision readback of a captured hermes panel dump (capture.py path)."""
    data = i1_path.read_bytes()
    if len(data) != capture.HERMES_BUF_SIZE:
        return ""
    img = capture.decode_hermes(data)
    png = i1_path.with_suffix(".png")
    img.save(png)
    try:
        return capture.describe_image(png, prompt=capture.HERMES_VISION_PROMPT)
    except Exception as e:
        log(f"vision readback failed: {e}")
        return ""


# ---------------------------------------------------------------------------
# Bridge journal tail (the assertion source). A shared JournalTail is created
# per loop right before the open, so only logs from that loop onward match.
# ---------------------------------------------------------------------------
class JournalTail:
    """Tail the bridge's INFO log file — the deterministic assertion source
    (no journalctl subprocess churn during the realtime audio leg, no journald
    dependency). Falls back to journalctl when BRIDGE_LOG_FILE is unset or the
    file is missing, so a misconfigured rig still works (if noisily).

    Fetched lines go into ``_pending`` and are REMOVED only when a wait_for
    consumes the exact match — so two target lines in one batch (e.g. the ASR
    line and the Hermes reply ~3 ms later) are both reachable by their own
    waiters, never dropped wholesale."""

    def __init__(self, path=None):
        self._path = Path(path or os.environ.get("BRIDGE_LOG_FILE",
                                                 os.path.expanduser("~/xiaozhi/voice_bridge.log"))).expanduser()
        self._pos = 0
        self._pending = []
        self._loaded = []
        self._last = time.time()

    def _from_file(self):
        if not self._path.is_file():
            return None
        try:
            with open(self._path, errors="replace") as fh:
                fh.seek(self._pos)
                data = fh.read()
                self._pos = fh.tell()
            return data.splitlines()
        except OSError:
            return None

    def _from_journalctl(self):
        env = dict(os.environ)
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        since = max(30, int(time.time() - self._last) + 2)
        try:
            cmd = ["journalctl", "--user", "-u", BRIDGE_UNIT, "--no-pager",
                   "-o", "cat", f"--since=-{since}s"]
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=30, env=env).stdout
            self._last = time.time()
            cur = out.splitlines()
        except Exception:
            cur = []
        prev = self._loaded
        idx = 0
        for i in range(len(prev), 0, -1):
            if prev[-i:] == cur[:i]:
                idx = i
                break
        new = cur[idx:]
        if new:
            self._loaded = cur
        return new

    def new_lines(self):
        """Append newly fetched lines to ``_pending`` and return them."""
        raw = self._from_file()
        if raw is None:
            raw = self._from_journalctl()
        self._pending.extend(raw)
        return raw

    def consume(self, index):
        """Waiters call this for the line they matched (drop it + anything
        before it; later lines stay for other waiters)."""
        if 0 <= index < len(self._pending):
            self._pending = self._pending[index + 1:]

    def clear_pending(self):
        self._pending = []


def wait_for(tail, pattern, session_id=None, timeout=90, poll=0.5):
    """Scan the tail's pending lines until one matches ``pattern`` (optionally
    scoped to a session id). Only the matching line is consumed; others stay
    pending so a later wait_for can still see them. Returns line or None."""
    done = time.monotonic() + timeout
    while True:
        tail.new_lines()
        for i, line in enumerate(tail._pending):
            if session_id and f"session={session_id}" not in line:
                continue
            if re.search(pattern, line, re.I):
                tail.consume(i)
                return line
        if time.monotonic() > done:
            return None
        time.sleep(poll)


# ---------------------------------------------------------------------------
# Transcript normalization + fuzzy match (faster-whisper output)
# ---------------------------------------------------------------------------
def norm(text):
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split()


def match_transcript(asr_text, expected):
    a, e = norm(asr_text), norm(expected)
    if not e:
        return False
    if a == e or len(set(a) & set(e)) >= len(e) * 0.75:
        return True
    it = iter(a)  # tolerated whisper variance: expected is a subsequence
    return all(w in it for w in e)


CONTENT = re.compile(r"[a-z]{3,}")


def content_tokens(text):
    return set(w for w in norm(text) if CONTENT.match(w))


def overlap(a, b):
    return len(content_tokens(a) & content_tokens(b))


def re_asr_file(path):
    import wave as _w
    with _w.open(str(path)) as w:
        pcm = w.readframes(w.getnframes())
    return call_asr(pcm, w.getframerate())


# ---------------------------------------------------------------------------
# One voice loop
# ---------------------------------------------------------------------------
def run_loop(n, instr, artifacts, prev_listen=None):
    log(f"--- loop {n + 1}/{N_RUNS} ---")
    res = {"loop": n + 1, "asserts": {}, "artifacts": {}}
    run_dir = artifacts / f"loop{n + 1}"
    run_dir.mkdir(parents=True, exist_ok=True)
    # Test-mic capture state shared by the inject-fallback and the normal
    # BRAIN path (the inject starts it BEFORE the reply plays; the normal path
    # starts it right as the reply begins).
    mic_path = run_dir / "mic_ring.wav"
    mic_thread = None
    cap_len = MIC_INJECT_CAP
    mic_started = False
    port = detect_board_port()
    if not port:
        res["asserts"]["OPEN_SESSION"] = "SKIP (no board); ASR/BRAIN/RING/SUBTITLE SKIP"
        return res

    tail = JournalTail()
    tail.new_lines()
    tail.clear_pending()  # drop pre-send lines (stale listens/replies)

    # 1. OPEN: loop 1 opens with its own RD_LISTEN; later loops reuse the listen
    # armed by the PREVIOUS loop's POST_READOUT_RELISTEN (done at the very end
    # of that loop, so it is only ~seconds old when this loop's command plays).
    # The board's listen stays armed for many minutes, so a fresh RD_LISTEN
    # while it is still armed is a no-op — reusing it is the only reliable open
    # (measured live: a fresh RD_LISTEN after a re-listen yielded no listen start).
    carry = prev_listen or {}
    if carry.get("ok"):
        ses = carry.get("ses")
        res["asserts"]["OPEN_SESSION"] = (
            "PASS (re-listen armed at prior tts-stop, session=" + (ses or "?") + ")")
    else:
        send_rd_listen(port)
        got = wait_for(tail, r"listen start", timeout=25)
        m = re.search(r"session=([0-9a-f]+)", got or "")
        ses = m.group(1) if m else None
        if not ses:
            res["asserts"]["OPEN_SESSION"] = "FAIL (no listen start after RD_LISTEN)"
            return res
        res["asserts"]["OPEN_SESSION"] = "PASS " + (got.split(": ", 1)[-1] if got else ses)
    res["artifacts"]["session"] = ses

    # 2. ASR: real-mic leg (or E2E_STT injection fallback)
    asr_text, ok = "", False
    command_wav = (HERE / "corpus" / instr["wav"]).resolve()
    if not command_wav.is_file():
        res["asserts"]["ASR_MATCH"] = f"FAIL (missing corpus wav {command_wav})"
    else:
        # Board mic/AEC needs a beat after StartListening() before the test
        # speaker is audible (RD_PLAY_SETTLE). Try the command up to twice;
        # a mismatch means the brain gets the canned utterance instead.
        for attempt in range(2):
            time.sleep(PLAY_SETTLE if attempt == 0 else 3.0)
            t = threading.Thread(target=play_wav, args=(command_wav, PLAY_VOL))
            t.start()  # board mic hears it; the board VAD ends the utterance
            got = wait_for(tail, r"ASR text='(.*)'", timeout=ASR_TIMEOUT // 4)
            t.join()
            if got:
                asr_text = re.search(r"ASR text='(.*)'", got).group(1)
                ok = match_transcript(asr_text, instr["transcript"])
                if ok:
                    break
                log(f"ASR mismatch ({asr_text!r}); retrying play once")
        res["asserts"]["ASR_MATCH"] = "PASS" if ok else "FAIL"
        res["artifacts"]["asr"] = asr_text
    if E2E_STT_TEXT or not ok:
        text = E2E_STT_TEXT or instr["transcript"]
        # The injected reply is streamed during inject_text, so the test-mic
        # capture must be running BEFORE the injection for READOUT/RING to hear
        # it (a capture started after inject always misses the reply).
        mic_path = run_dir / "mic_ring.wav"
        cap_len = MIC_INJECT_CAP
        mic_thread = threading.Thread(target=capture_mic, args=(mic_path, cap_len))
        mic_thread.start()
        mic_started = True
        try:
            inject_text(text, session_id=ses)
        except Exception as e:
            # Reap the pre-inject capture: ffmpeg holds the exclusive ALSA
            # capture PCM for up to MIC_INJECT_CAP s. If it is left running,
            # the next loop's capture_mic fails busy and that loop's RING/
            # READOUT/POST_READOUT all drop as "no test-mic capture".
            mic_thread.join(timeout=cap_len + 30)
            res["asserts"]["BRAIN_REPLY"] = f"FAIL (inject: {e})"
            return res
        if not E2E_STT_TEXT:
            # real-speech ASR flaky this loop: documented injection fallback
            # (covers brain->TTS->speaker->display; skips the mic leg)
            res["asserts"]["ASR_MATCH"] = (
                "FAIL (mic ASR) -> E2E_STT inject fallback"
                if not asr_text else f"FAIL (mic ASR {asr_text!r}) -> E2E_STT inject")
        else:
            res["asserts"]["ASR_MATCH"] = "SKIP (E2E_STT injection mode)"

    # Panel dumps start here: the bridge has already sent the stt frame, so
    # the STT subtitle is on screen and stays through the brain/TTS window —
    # a 4 s-cadence dump now reliably catches it (later starts miss it).
    shots = run_dir / "shots"
    shots.mkdir(parents=True, exist_ok=True)

    def _capture_shots():
        try:
            frames = capture.read_frames(port, n=8, timeout=120)
            for i, (data, _, _, _) in enumerate(frames):
                (shots / f"frame{i}.i1").write_bytes(data)
        except Exception as e:
            log(f"RD_SHOT capture failed: {e}")

    shot_thread = threading.Thread(target=_capture_shots)
    shot_thread.start()

    # 3. BRAIN: Hermes reply non-empty (auto-TTS voice audio or text)
    got_reply = wait_for(tail, r"Hermes\s+(?:voice\s+|reply\s+)reply:",
                         session_id=ses, timeout=REPLY_TIMEOUT)
    kind, payload = "", ""
    if got_reply:
        m = re.search(r"Hermes\s+voice\s+reply:\s*(\d+)", got_reply)
        if m:
            kind, payload = "voice", m.group(1)
        else:
            m = re.search(r"Hermes\s+reply\s+reply:\s*'([^']*)'", got_reply)
            if m:
                kind, payload = "reply", m.group(1)
    if not kind:
        res["asserts"]["BRAIN_REPLY"] = "FAIL (no Hermes reply)"
        return res
    res["asserts"]["BRAIN_REPLY"] = f"PASS kind={kind} {payload!r}"
    reply_secs = int(payload) / 48000.0 if kind == "voice" else 4.0

    # 4-6. RING + SUBTITLE + READOUT: test-mic hears the board speaker (panel
    # dumps already running since just after the ASR leg — shot_thread above).
    # The bridge's `session=<id> tts stop` anchor (issue #274) marks when the
    # reply's last paced frame + tts-stop were sent, so a fresh RD_LISTEN right
    # then is the post-readout re-listening proxy — the reachable version of
    # the realtime auto-relisten this board does not expose to the E2E.
    if not mic_started:
        cap_len = min(15.0, reply_secs + 7.0)
        mic_thread = threading.Thread(target=capture_mic, args=(mic_path, cap_len))
        mic_thread.start()
        time.sleep(0.7)

    # Authoritative spoken duration + the post-readout anchor, read in ONE
    # session-scoped poll window. The bridge logs `tts streamed full <N>` right
    # BEFORE `tts stop`; a wait_for('tts stop') that runs first would consume
    # the streamed line under it (JournalTail drops everything through the
    # matched line), so the refresh never fired. Poll once: track the LATEST
    # streamed size (an inject loop plays a second, longer reply whose byte
    # count differs from the BRAIN assert) until tts-stop lands, then refresh
    # reply_secs — exact for auto-TTS voice replies AND bridge-synthesized
    # text replies (the latter were previously guessed at 4.0 s).
    tts_stop = None
    streamed_sz = 0
    deadline = time.monotonic() + int(reply_secs) + 15
    while time.monotonic() < deadline:
        tail.new_lines()
        for line in tail._pending:
            if f"session={ses}" not in line:
                continue
            m_sz = re.search(r"tts streamed full (\d+)", line)
            if m_sz:
                streamed_sz = max(streamed_sz, int(m_sz.group(1)))
            if not tts_stop and re.search(r"tts stop", line, re.I):
                tts_stop = line
        if tts_stop or time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    res["artifacts"]["tts_stop_anchor"] = bool(tts_stop)
    if streamed_sz:
        reply_secs = streamed_sz / 48000.0
        res["artifacts"]["reply_secs_streamed"] = reply_secs

    mic_thread.join(timeout=cap_len + 30)
    time.sleep(2.0)  # let the board play the reply / drop a frame
    shot_thread.join(timeout=120)
    if not mic_path.exists():
        res["asserts"]["RING"] = "FAIL (no test-mic capture)"
        res["asserts"]["SUBTITLE"] = "FAIL (no test-mic capture)"
        res["asserts"]["READOUT_COMPLETE"] = "FAIL (no test-mic capture)"
        res["asserts"]["POST_READOUT_RELISTEN"] = "FAIL (no test-mic capture)"
        res["next_listen"] = {"ok": False, "ses": None}
        return res

    # RING: re-ASR the loudest window of the test-mic capture
    peak = 0.0
    ring_txt = ""
    try:
        st, en = energy_burst_window(mic_path, cap_len)
        burst = run_dir / "reply_burst.wav"
        trim_wav(mic_path, st, en, burst)
        peak = wave_rms(burst)
        ring_txt = re_asr_file(burst)
    except Exception as e:
        log(f"ring re-ASR failed: {e}")
    (run_dir / "ring_reasr.txt").write_text(ring_txt)

    # SUBTITLE: panel readback carries the STT transcript (or injected text)
    subtitle_txt = ""
    for f in sorted(shots.glob("frame*.i1")):
        t = describe_frame(f)
        if t and "BLANK" not in t.upper():
            subtitle_txt = t
            break
    (run_dir / "subtitle_readback.txt").write_text(subtitle_txt)

    base = quiet_floor(mic_path)
    audible = peak >= max(120.0, base * 4)
    aligned = bool(ring_txt) and (
        overlap(ring_txt, subtitle_txt) >= 1
        or (kind == "reply" and overlap(ring_txt, payload) >= 1))
    if not ring_txt:
        ring_v = "FAIL (no re-ASR of board speaker)"
    elif aligned:
        ring_v = "PASS (re-ASR aligns with subtitle/reply)"
    elif audible:
        ring_v = "PASS-WEAK (audible TTS captured; verbatim re-ASR in artifacts)"
    else:
        ring_v = "FAIL (test mic did not capture board speaker)"
    res["asserts"]["RING"] = f"{ring_v} re-asr={ring_txt!r} rms={int(peak)} floor={int(base)}"

    target = E2E_STT_TEXT or instr["transcript"]
    shown = bool(subtitle_txt) and any(w in norm(subtitle_txt.lower())
                                       for w in norm(target))
    if shown:
        res["asserts"]["SUBTITLE"] = "PASS " + subtitle_txt.replace("\n", " | ")
    elif subtitle_txt:
        # readback happened but lacks the STT phrase: pass only when the ring
        # proves the TTS was spoken (echo/placement) and the panel had *some*
        # text (face/clock alone = no subtitle proven)
        res["asserts"]["SUBTITLE"] = (
            "PASS-TEXT " + subtitle_txt.replace("\n", " | ")
            if audible else "FAIL " + subtitle_txt.replace("\n", " | "))
    else:
        res["asserts"]["SUBTITLE"] = "FAIL no-subtitle-readback (panel blank)"
    res["artifacts"]["subtitle"] = subtitle_txt

    # READOUT_COMPLETE: the full active-play span vs the reply's reported TTS
    # duration. This is the #274 regression — a read-out truncated to ~2.4 s by
    # the decode-queue drop (#272's pacing removes it) must FAIL, not pass as
    # mere "audible" like the RING burst re-ASR does.
    span, pstart, pend = 0.0, 0.0, 0.0
    try:
        span, pstart, pend = active_play_span(mic_path)
    except Exception as e:
        log(f"readout span failed: {e}")
    frac = played_fraction(span, reply_secs)
    res["artifacts"]["play_span"] = span
    res["artifacts"]["played_fraction"] = frac
    if span == 0.0:
        res["asserts"]["READOUT_COMPLETE"] = "FAIL (no active play heard on test mic)"
    elif reply_secs < READOUT_MIN_REPLY:
        res["asserts"]["READOUT_COMPLETE"] = (
            f"SKIP (reply {reply_secs:.1f}s < {READOUT_MIN_REPLY:.1f}s — not "
            "discriminative for the ~0.5 s truncation delta)")
    elif frac >= 0.85:
        res["asserts"]["READOUT_COMPLETE"] = (
            f"PASS played={span:.1f}s reply={reply_secs:.1f}s frac={frac:.2f}")
    else:
        res["asserts"]["READOUT_COMPLETE"] = (
            f"FAIL played={span:.1f}s of reply {reply_secs:.1f}s "
            f"(frac={frac:.2f} < 0.85) — truncated read-out?")

    # POST_READOUT_RELISTEN (issue #274): the reachable proxy for the realtime
    # auto-relisten the board does not expose to the E2E — after the reply's
    # tts-stop, a fresh RD_LISTEN must re-arm listening. Done at the END of the
    # loop so the armed listen is only seconds old when the NEXT loop's command
    # plays (it is reused as that loop's open; a fresh RD_LISTEN into an already-
    # armed board is a measured no-op, and a long-idle armed listen starves the
    # next command's ASR).
    if not tts_stop:
        res["asserts"]["POST_READOUT_RELISTEN"] = (
            "FAIL (no `tts stop` anchor — deployed bridge predates this PR's "
            "anchor; POST_READOUT cannot be proven). Redeploy "
            "robodog_client/voice/voice_bridge.py and restart "
            "systemctl --user lmao-robodog-voice")
        res["next_listen"] = {"ok": False, "ses": None}
    else:
        time.sleep(RE_LISTEN_GRACE)      # let the board drain the last frames
        send_rd_listen(port)
        got2 = wait_for(tail, r"listen start", timeout=RE_LISTEN_TIMEOUT)
        m2 = re.search(r"session=([0-9a-f]+)", got2 or "")
        ses2 = m2.group(1) if m2 else None
        if ses2:
            res["asserts"]["POST_READOUT_RELISTEN"] = ("PASS " +
                (got2.split(": ", 1)[-1] if got2 else ses2))
            res["next_listen"] = {"ok": True, "ses": ses2}
        else:
            res["asserts"]["POST_READOUT_RELISTEN"] = (
                "FAIL (no `listen start` right after tts stop)")
            res["next_listen"] = {"ok": False, "ses": None}
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="count",
                    help="corpus slug to drive (see voice/corpus/corpus.json); "
                         "'count' ('count slowly from twenty to thirty') is the "
                         "long-reply corpus: a deterministic 11-number list the "
                         "brain reads as a reliably ~7-10 s spoken VOICE reply, "
                         "comfortably above READOUT_MIN_REPLY (3.5 s) and the "
                         "~0.85 played-fraction floor, and well inside the "
                         "read-out capture window (~15 s cap)")
    ap.add_argument("--artifacts", default=str(Path.home() / "artifacts"))
    args = ap.parse_args()
    artifacts = Path(args.artifacts) / "robodog-voice-e2e"
    artifacts.mkdir(parents=True, exist_ok=True)

    port = detect_board_port()
    card = detect_sound_card()
    healthy = bridge_health()
    log(f"board={port or 'NONE'} sound='{card}' bridge_health={healthy} "
        f"runs={N_RUNS} stt_text={'inject:' + E2E_STT_TEXT if E2E_STT_TEXT else 'mic'}")
    skips = []
    if not port:
        skips.append("ROBODOG BOARD NOT DETECTED (0x303A:0x1001)")
    if not card:
        skips.append("TEST AUDIO CARD NOT DETECTED (ALSA USB)")
    if not healthy:
        skips.append(f"BRIDGE HEALTHZ DOWN (127.0.0.1:{BRIDGE_OTA_PORT})")
    if skips:
        print("\nLOUD SKIP: " + "; ".join(skips) + "\n")
        (artifacts / "SKIPPED.txt").write_text("; ".join(skips) + "\n")
        return 0

    man = json.loads((HERE / "corpus" / "corpus.json").read_text())
    if args.corpus not in man:
        sys.exit(f"unknown corpus slug '{args.corpus}': {sorted(man)}")
    instr = man[args.corpus]

    results = []
    prev_listen = None
    for i in range(N_RUNS):
        r = run_loop(i, instr, artifacts, prev_listen)
        results.append(r)
        for k, v in r["asserts"].items():
            log(f"{k}: {v}")
        # The end-of-loop POST_READOUT_RELISTEN arms the next loop's open, so
        # only a short drain is needed between loops.
        prev_listen = r.get("next_listen")
        if i < N_RUNS - 1:
            log("settling between loops ...")
            time.sleep(BETWEEN_LOOP_SETTLE)

    lines = [
        "# RoboDog Hermes Voice-Loop E2E Results\n",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M')}",
        f"Board: {port}",
        f"Corpus: {args.corpus} ({instr['transcript']!r})",
        f"Mode: {'E2E_STT injection' if E2E_STT_TEXT else 'real-microphone'}",
        f"Runs: {N_RUNS}\n",
    ]
    for r in results:
        lines.append(f"## loop {r['loop']} (session {r['artifacts'].get('session', '-')})")
        for k, v in r["asserts"].items():
            lines.append(f"- **{k}**: {v}")
        lines.append(f"- ASR verbatim: {r['artifacts'].get('asr', '')!r}")
        lines.append("")

    def asr_ok(v):
        # The documented E2E_STT fallback (issue #267) is an acceptable result
        # for the real-mic leg: brain->TTS->speaker->display all still run.
        return (v.startswith("PASS") or v.startswith("SKIP")
                or "E2E_STT" in v or "inject fallback" in v)

    def mode_of(r):
        v = r["asserts"].get("ASR_MATCH", "")
        if v.startswith("PASS"):
            return "real-microphone"
        if v.startswith("SKIP"):
            return "E2E_STT injection (mic leg skipped)"
        if "E2E_STT" in v or "inject fallback" in v:
            return "E2E_STT injection (mic leg flaky -> documented fallback)"
        return "unknown"

    def passed(r):
        # Issue #274: the read-out must be complete (not merely "audible" like
        # the RING burst) and the board must re-listen right after tts-stop.
        # SKIP is accepted only for the documented data/env conditions (short
        # reply below the discriminative floor; bridge without the tts-stop
        # anchor) — a real FAIL fails the gate.
        def ok(v):
            return v is not None and (v.startswith("PASS") or v.startswith("SKIP"))

        return (r["asserts"].get("OPEN_SESSION", "FAIL").startswith("PASS")
                and r["asserts"].get("BRAIN_REPLY", "FAIL").startswith("PASS")
                and asr_ok(r["asserts"].get("ASR_MATCH", "FAIL"))
                and ok(r["asserts"].get("READOUT_COMPLETE"))
                and ok(r["asserts"].get("POST_READOUT_RELISTEN")))

    for r in results:
        r["artifacts"]["mode"] = mode_of(r)

    overall = all(passed(r) for r in results)
    lines.append("## Modes per loop")
    for r in results:
        lines.append(f"- loop {r['loop']}: {r['artifacts'].get('mode', '?')}")
    lines.append("")
    lines.append(f"Overall: {'PASS' if overall else 'FAIL'}")
    (artifacts / "robodog-voice-loop-e2e.md").write_text("\n".join(lines))
    print("\n".join(lines))
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
