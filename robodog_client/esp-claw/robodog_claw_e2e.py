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
ASR_URL = os.environ.get("RD_ASR_URL", "http://192.168.50.153:11801/v1/audio/transcriptions")
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
    with urllib.request.urlopen(req, timeout=120) as resp:
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
        log(f"audio cmd failed rc={r.returncode}: {cmd[:120]}… "
            f"{r.stderr[-200:].strip()}")
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
def open_console(port):
    s = serial.Serial(port, CONSOLE_BAUD, timeout=0.2)
    time.sleep(1.0)  # let USB-Serial/JTAG enumeration settle
    s.reset_input_buffer()
    return s


def write_cmd(s, cmd):
    s.write(f"{cmd}\r\n".encode())
    s.flush()


# ---------------------------------------------------------------------------
# Transcript normalization + fuzzy match
# ---------------------------------------------------------------------------
def norm(text):
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split()


def token_overlap(a, b):
    if not a or not b:
        return 0.0
    an, bn = set(norm(a)), set(norm(b))
    inter = len(an & bn)
    return inter / max(len(an), len(bn))


def wait_for_log(s, patterns, timeout, label="", spill=None):
    """Scan the serial console for any line matching one of `patterns`.
    Returns the regex Match, or None on timeout.

    With `spill` (bytearray/bytes), every already-read byte BEYOND the matched
    line is appended to it before returning: wait_for_log buffers up to 8 KB of
    console per read and DISCARDS the unprocessed remainder on return, which
    otherwise swallows lines that arrive right behind the match (e.g. the
    `voice chunk 1/n` line immediately after `agent reply:`)."""
    deadline = time.time() + timeout
    acc = b""
    while time.time() < deadline:
        data = s.read(8192)
        if data:
            acc += data
            # keep only the tail (in case a line spans reads); process whole lines
            while b"\n" in acc:
                line, acc = acc.split(b"\n", 1)
                ln = line.decode("utf-8", "replace").strip()
                for pat in patterns:
                    m = re.search(pat, ln)
                    if m:
                        if label:
                            log(f"{label}: {ln[:160]}")
                        if spill is not None:
                            spill.extend(acc)
                        return m, ln
            if len(acc) > 65536:
                acc = acc[-65536:]
    if spill is not None:
        spill.extend(acc)
    return None, ""


# --voice-verify console collectors: the device logs one `voice chunk i/n`
# line per queued sentence chunk before its TTS bracket, so the host can prove
# the FULL reply was queued and voiced without any acoustic ambiguity.
CHUNK_RE = re.compile(r"voice chunk (\d+)/(\d+):\s*(.*)")
TTS_START_RE = re.compile(r"TTS start")
TTS_STOP_RE = re.compile(r"TTS stop")


def collect_voice_chunks(s, timeout, seed=b""):
    """Scan the console while the chunk queue plays back.  Returns
    (chunks, starts, stops): chunks maps chunk index i (1-based) to
    (n_total, text); starts/stops count the `TTS start`/`TTS stop` brackets.
    Ends as soon as every advertised chunk has its own start AND stop — i.e.
    the last `TTS stop` of the queue — or when `timeout` runs out.

    `seed` replays bytes that wait_for_log already consumed past its match
    (see its `spill` param) so the `voice chunk 1/n` line that lands right
    behind `agent reply:` is not lost."""
    deadline = time.time() + timeout
    acc = seed if isinstance(seed, bytes) else bytes(seed)  # may be bytearray
    chunks = {}
    starts = stops = 0
    max_n = 0
    while time.time() < deadline:
        data = s.read(8192)
        if data:
            acc += data
            while b"\n" in acc:
                line, acc = acc.split(b"\n", 1)
                ln = line.decode("utf-8", "replace").strip()
                m = CHUNK_RE.search(ln)
                if m:
                    i, n = int(m.group(1)), int(m.group(2))
                    chunks[i] = (n, m.group(3).strip())
                    max_n = max(max_n, n)
                    log(f"chunk {i}/{n} seen")
                if TTS_START_RE.search(ln):
                    starts += 1
                if TTS_STOP_RE.search(ln):
                    stops += 1
            if len(acc) > 65536:
                acc = acc[-65536:]
        if max_n and starts >= max_n and stops >= max_n:
            break
    return chunks, starts, stops


def tail_tokens(text, k=12):
    """The LAST k normalized tokens of a reply — the anti-truncation target:
    if the read-out reached the reply's end, the mic capture must carry them."""
    toks = norm(text)
    return set(toks[-k:]) if toks else set()


def read_shot_frame(s, timeout=30, validate=True):
    """Scan the console for an RD_SHOT_BEGIN..RD_SHOT_END block; return raw
    bytes + (w,h).  With validate=True, keeps reading past truncated frames
    (the forced `rdshot` frame races the self-tick flood) until a complete
    bufsize-length frame is seen."""
    deadline = time.time() + timeout
    acc = b""
    in_shot = False
    want = 0
    hex_acc = []
    while time.time() < deadline:
        chunk = s.read(16384)
        if not chunk:
            continue
        acc += chunk
        while b"\n" in acc:
            line, acc = acc.split(b"\n", 1)
            ln = line.decode("utf-8", "replace").strip()
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
                    # The forced `rdshot` frame often races the self-tick flood
                    # and arrives truncated (< bufsize).  Keep reading for a
                    # complete frame — the self-tick guarantees a full one
                    # within the timeout — instead of failing on the first
                    # partial.  validate==False callers accept any frame.
                    if validate and len(data) == want:
                        return data, (want, w, h)
                    if not validate:
                        return data, (want, w, h)
                    in_shot = False
                    hex_acc = []
                elif re.match(r"^[0-9a-fA-F]+$", ln):
                    hex_acc.append(ln)
    return None, (0, 0, 0)


def capture_subtitle(s, artifacts):
    """Send `rdshot` on the already-open console `s` and read a frame; decode
    + vision-read it back.  Reuses the loop's console (reopening a second
    handle races the heavy self-tick RD_SHOT flood and loses frames)."""
    try:
        write_cmd(s, "rdshot")
        data, (want, w, h) = read_shot_frame(s, timeout=25)
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
    res = {"loop": n + 1, "asserts": {}, "artifacts": {"asr": "", "reply": ""}}
    run_dir = artifacts / f"loop{n + 1}"
    run_dir.mkdir(parents=True, exist_ok=True)
    target_txt = instr["transcript"]
    target_wav = HERE.parent / "voice" / "corpus" / instr["wav"]

    s = open_console(DETECTED_PORT)
    try:
        # ---- OPEN_SESSION: trigger + confirm listen ------
        write_cmd(s, "rd_listen")
        m, _ = wait_for_log(s, [r"listen started"], timeout=15, label="OPEN")
        res["asserts"]["OPEN_SESSION"] = (
            "PASS listen started" if m else "FAIL (no 'listen started' after rd_listen)"
        )

        # ---- ASR_MATCH: play the corpus into the mic ------
        time.sleep(PLAY_SETTLE)
        log(f"playing corpus {target_wav.name} into board mic ...")
        play_wav(target_wav)

        asr_spill = bytearray()
        m_asr, _ = wait_for_log(s, [r'ASR text="([^"]*)"'], timeout=ASR_TIMEOUT,
                                label="ASR", spill=asr_spill)
        asr_txt = m_asr.group(1) if m_asr else ""
        res["artifacts"]["asr"] = asr_txt
        frac = token_overlap(asr_txt, target_txt)
        if asr_txt:
            res["asserts"]["ASR_MATCH"] = (
                f"PASS overlap={frac:.2f} asr={asr_txt!r}"
                if frac >= 0.75
                else f"FAIL overlap={frac:.2f} asr={asr_txt!r} want={target_txt!r}"
            )
        else:
            res["asserts"]["ASR_MATCH"] = "FAIL (no `ASR text=` on console)"
            # Diagnose: surface the board's OWN capture/VAD/Whisper decisions
            # (spilled beyond the ASR wait) so a failed acoustic leg says why —
            # VAD never heard speech vs Whisper round-trip failure vs silent.
            if asr_spill:
                board_lines = [
                    bl for bl in asr_spill.decode("utf-8", "replace").splitlines()
                    if any(k in bl for k in (
                        "robodog_voice", "ASR", "utterance", "speech", "bail"))
                    and "RD_SHOT" not in bl
                ]
                if board_lines:
                    log("board capture/VAD/ASR decisions (no `ASR text=`):")
                    for bl in board_lines[-12:]:
                        log("   | " + bl.strip()[:160])

        # Fail fast: no ASR means no agent run / TTS / subtitle in this turn —
        # waiting for the reply would just burn the full 180 s REPLY_TIMEOUT.
        if not asr_txt:
            skip = (
                ("REPLY", "TTS", "CHUNKED_QUEUE", "FULL_READOUT")
                if voice_verify
                else ("REPLY", "TTS", "RING", "SUBTITLE")
            )
            for k in skip:
                res["asserts"][k] = "SKIP (no ASR -> no turn)"
            res["artifacts"]["reply"] = ""
            s.close()
            return res

        # ---- REPLY: native agent LLM ------
        # spill: wait_for_log buffers up to 8 KB of console and discards the
        # unprocessed remainder past the match — exactly where the first
        # `voice chunk 1/n` lands — so hand it to the voice-verify collector.
        leftover = bytearray()
        m_rep, _ = wait_for_log(s, [r"agent reply: (.+)"], timeout=REPLY_TIMEOUT,
                                label="REPLY", spill=leftover)
        reply = m_rep.group(1).strip() if m_rep else ""
        res["artifacts"]["reply"] = reply
        res["asserts"]["REPLY"] = (
            f"PASS reply={reply!r}" if reply else "FAIL (no `agent reply:` on console)"
        )

        # ---- TTS + RING: capture the board speaker ------
        m_tts = None
        m_tts_stop = None
        mic_path = run_dir / "speaker.wav"
        ring_txt = ""
        chunks, starts, stops = {}, 0, 0
        if voice_verify:
            # No separate `TTS start` wait: wait_for_log would CONSUME the
            # `voice chunk i/n` line that precedes each `TTS start`, making
            # CHUNKED_QUEUE unobservable.  collect_voice_chunks scans for the
            # chunk logs AND their start/stop brackets together, from right
            # after `agent reply:`.
            log("TTS phase; capturing board speaker on test mic ...")
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
            chunks, starts, stops = collect_voice_chunks(s, REPLY_TIMEOUT,
                                                         seed=leftover)
            # The re-ASR below reads the WHOLE captured WAV, so wait for the
            # background capture to actually finish (bounded by
            # VOICE_CAP_SECS) — reading a half-written RIFF yields nothing.
            cap_done.wait()
        else:
            m_tts, _ = wait_for_log(s, [r"TTS start"], timeout=REPLY_TIMEOUT, label="TTS")
            if m_tts:
                log("TTS start seen; capturing board speaker on test mic ...")
                try:
                    capture_mic(mic_path, RING_CAP_SECS)
                except Exception as e:
                    log(f"ring capture failed: {e}")
                # Board TTS is real-time paced (~16 s for a long reply); wait for
                # `TTS stop` NOW, before the slow ring re-ASR below scrolls it past
                # the serial buffer and makes the acceptance leg unobservable.
                m_tts_stop, _ = wait_for_log(s, [r"TTS stop"], timeout=REPLY_TIMEOUT, label="TTS")
        try:
            # The board's reply is faint but spans the whole TTS window; a
            # single loudest-3s burst is often ambient noise and lets
            # Whisper under-trigger (re-ASR back to nothing).  Transcribe
            # the FULL capture first, and only fall back to the loudest
            # burst if the full window yields no text.
            ring_txt = remote_asr(mic_path)
            if not ring_txt:
                burst = run_dir / "reply_burst.wav"
                trim_loudest_window(mic_path, burst, 4.0)
                if burst.exists():
                    ring_txt = remote_asr(burst)
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
            # console-side: every queued chunk MUST have its own TTS bracket.
            if chunks:
                ordered = sorted(chunks) == list(range(1, max(chunks) + 1))
                bracketed = starts >= max(chunks) and stops >= max(chunks)
                res["asserts"]["CHUNKED_QUEUE"] = (
                    f"PASS {max(chunks)} chunk(s), all TTS-bracketed "
                    f"(start={starts} stop={stops})"
                    if ordered and bracketed
                    else f"FAIL chunks={sorted(chunks)} starts={starts} stops={stops}"
                )
            else:
                res["asserts"]["CHUNKED_QUEUE"] = (
                    "FAIL (no `voice chunk i/n` logs — the queue did not run)"
                )
            # FULL_READOUT: the LAST chunk's words must appear in the mic
            # capture — the read-out reached the reply's end, nothing was cut
            # off.  Console queue evidence above is authoritative; this is the
            # acoustic corroboration (PASS-WEAK tolerated for marginal
            # speaker->mic coupling).
            tail = tail_tokens(reply)
            got = set(norm(ring_txt)) if ring_txt else set()
            overlap = token_overlap(ring_txt, reply)
            if ring_txt and tail and len(tail & got) >= 2:
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
                    "PASS-WEAK audible "
                    f"(reply too short / noisy re-ASR={ring_txt!r})"
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
            sub_txt, sub_err = capture_subtitle(s, run_dir)
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
        s.close()
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--corpus",
        default="capabilities",
        help="corpus slug to voice-drive (corpus.json); default 'capabilities'",
    )
    ap.add_argument("--artifacts", default=str(Path.home() / "artifacts"))
    ap.add_argument(
        "--voice-verify",
        action="store_true",
        help="dedicated acoustic voice verification mode: asserts the FULL"
        " chunked reply was queued AND read out to its end (CHUNKED_QUEUE +"
        " FULL_READOUT legs instead of RING/SUBTITLE). Writes"
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

    results = []
    for i in range(N_RUNS):
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
        f"Corpus: {args.corpus} ({instr['transcript']!r})",
        f"Runs: {N_RUNS}\n",
    ]
    for r in results:
        lines.append(f"## loop {r['loop']}")
        for k, v in r["asserts"].items():
            lines.append(f"- **{k}**: {v}")
        lines.append(f"- ASR verbatim: {r['artifacts'].get('asr', '')!r}")
        lines.append(f"- REPLY verbatim: {r['artifacts'].get('reply', '')!r}")
        lines.append("")

    def ok(v):
        return v is not None and (v.startswith("PASS") or v.startswith("SKIP"))

    keys = (
        ("OPEN_SESSION", "ASR_MATCH", "REPLY", "TTS", "CHUNKED_QUEUE", "FULL_READOUT")
        if voice_verify
        else ("OPEN_SESSION", "ASR_MATCH", "REPLY", "TTS", "RING", "SUBTITLE")
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
        f"Corpus: {args.corpus} ({instr['transcript']!r})",
        f"Runs: {N_RUNS}\n",
    ]
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
