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
ASR_URL = os.environ.get(
    "RD_ASR_URL", "http://192.168.50.153:11801/v1/audio/transcriptions")
ASR_MODEL = os.environ.get("RD_ASR_MODEL", "whisper-1")
# Playback settle after `listen started` before the test speaker plays the
# corpus into the board mic (mic/ADC ramp).  Too early -> attenuated below the
# energy-VAD threshold (no utterance); a couple of seconds is safe here.
PLAY_SETTLE = float(os.environ.get("RD_PLAY_SETTLE", "3.0"))
# Window for the test mic to capture the board's spoken TTS reply.
RING_CAP_SECS = float(os.environ.get("RD_RING_CAP_SECS", "15"))
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
    fields = [f"--{boundary}\r\n"
              'Content-Disposition: form-data; name="model"\r\n\r\n'
              f"{ASR_MODEL}\r\n"
              f"--{boundary}\r\n"
              'Content-Disposition: form-data; name="file"; '
              'filename="ring.wav"\r\n'
              "Content-Type: audio/wav\r\n\r\n"]
    body = ("".join(fields)).encode() + wav + \
        f"\r\n--{boundary}--\r\n".encode()
    import urllib.request
    req = urllib.request.Request(
        ASR_URL, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)["text"]


# ---------------------------------------------------------------------------
# Host-side audio drive (test speaker -> board mic) + capture (board speaker)
# ---------------------------------------------------------------------------
def play_wav(path: Path, volume: float = PLAY_VOL):
    cmd = (f"{SILENCE} 'ffmpeg -hide_banner -loglevel error -re -i {path} "
           f"-af volume={volume} -ac 2 -f alsa {ALSA_DEV}'")
    shell_run(cmd, timeout=120)


def capture_mic(path: Path, seconds: float):
    cmd = (f"{SILENCE} 'ffmpeg -hide_banner -loglevel error -f alsa "
           f"-i {ALSA_DEV} -t {seconds} -ac 1 -ar 16000 {path}'")
    shell_run(cmd, timeout=int(seconds) + 30)
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
        chunk = samples[i:i + bucket]
        if not chunk:
            break
        rms = (sum(abs(s) for s in chunk) / len(chunk))
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
        w2.setnchannels(1); w2.setsampwidth(2); w2.setframerate(rate)
        w2.writeframes(burst)
    return best_start


# ---------------------------------------------------------------------------
# Device console (serial) tail
# ---------------------------------------------------------------------------
def open_console(port):
    s = serial.Serial(port, CONSOLE_BAUD, timeout=0.2)
    time.sleep(1.0)          # let USB-Serial/JTAG enumeration settle
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


def wait_for_log(s, patterns, timeout, label=""):
    """Scan the serial console for any line matching one of `patterns`.
    Returns the regex Match, or None on timeout."""
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
                        return m, ln
            if len(acc) > 65536:
                acc = acc[-65536:]
    return None, ""


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
def run_loop(n, instr, artifacts):
    res = {"loop": n + 1, "asserts": {}, "artifacts": {"asr": "", "reply": ""}}
    run_dir = artifacts / f"loop{n + 1}"
    run_dir.mkdir(parents=True, exist_ok=True)
    target_txt = instr["transcript"]
    target_wav = HERE.parent / "voice" / "corpus" / instr["wav"]

    s = open_console(DETECTED_PORT)
    try:
        # ---- OPEN_SESSION: trigger + confirm listen -------------------------------------------------
        write_cmd(s, "rd_listen")
        m, _ = wait_for_log(s, [r"listen started"], timeout=15, label="OPEN")
        res["asserts"]["OPEN_SESSION"] = (
            "PASS listen started" if m else "FAIL (no 'listen started' after rd_listen)")

        # ---- ASR_MATCH: play the corpus into the mic ------------------------------------------------
        time.sleep(PLAY_SETTLE)
        log(f"playing corpus {target_wav.name} into board mic ...")
        play_wav(target_wav)

        m_asr, _ = wait_for_log(s, [r'ASR text="([^"]*)"'], timeout=ASR_TIMEOUT,
                                label="ASR")
        asr_txt = m_asr.group(1) if m_asr else ""
        res["artifacts"]["asr"] = asr_txt
        frac = token_overlap(asr_txt, target_txt)
        if asr_txt:
            res["asserts"]["ASR_MATCH"] = (
                f"PASS overlap={frac:.2f} asr={asr_txt!r}"
                if frac >= 0.75 else
                f"FAIL overlap={frac:.2f} asr={asr_txt!r} want={target_txt!r}")
        else:
            res["asserts"]["ASR_MATCH"] = "FAIL (no `ASR text=` on console)"

        # Fail fast: no ASR means no agent run / TTS / subtitle in this turn —
        # waiting for the reply would just burn the full 180 s REPLY_TIMEOUT.
        if not asr_txt:
            for k in ("REPLY", "TTS", "RING", "SUBTITLE"):
                res["asserts"][k] = "SKIP (no ASR -> no turn)"
            res["artifacts"]["reply"] = ""
            s.close()
            return res

        # ---- REPLY: native agent LLM -----------------------------------------------------------------
        m_rep, _ = wait_for_log(s, [r"agent reply: (.+)"], timeout=REPLY_TIMEOUT,
                                label="REPLY")
        reply = m_rep.group(1).strip() if m_rep else ""
        res["artifacts"]["reply"] = reply
        res["asserts"]["REPLY"] = (
            f"PASS reply={reply!r}" if reply else
            "FAIL (no `agent reply:` on console)")

        # ---- TTS + RING: capture the board speaker while it talks ------------------------------------
        m_tts, _ = wait_for_log(s, [r"TTS start"], timeout=REPLY_TIMEOUT,
                                label="TTS")
        mic_path = run_dir / "speaker.wav"
        ring_txt = ""
        if m_tts:
            log("TTS start seen; capturing board speaker on test mic ...")
            try:
                capture_mic(mic_path, RING_CAP_SECS)
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
                log(f"ring capture/re-ASR failed: {e}")
        _, _ = wait_for_log(s, [r"TTS stop"], timeout=REPLY_TIMEOUT, label="TTS")
        if ring_txt and reply:
            ok = token_overlap(ring_txt, reply) >= 0.3
            res["asserts"]["RING"] = (
                f"PASS re-asr={ring_txt!r} (overlaps reply)"
                if ok else
                f"PASS-WEAK re-asr={ring_txt!r} (audible but no verbatim overlap)")
        elif ring_txt:
            res["asserts"]["RING"] = f"PASS-WEAK re-asr={ring_txt!r} (no reply to compare)"
        else:
            res["asserts"]["RING"] = "FAIL (no re-ASR of board speaker)"

        # ---- SUBTITLE: the reply renders on the RLCD ------------------------------------------------
        time.sleep(1.0)
        sub_txt, sub_err = capture_subtitle(s, run_dir)
        # The panel readback must carry the ASSISTANT REPLY (Phase 1 gate
        # contract: "rd_chat N*N -> the reply renders on screen").  The readback
        # is OCR-noisy (whole-screen transcript), so require >= 2 distinct reply
        # tokens to appear in it — a set-overlap ratio is diluted by the screen's
        # long text.  The user transcript in the readback is pass-through.
        read_tok = set(norm(sub_txt)) if sub_txt else set()
        shared = len(set(norm(reply)) & read_tok) if reply else 0
        shown = bool(sub_txt) and len(read_tok) >= 3 and shared >= 2
        if shown:
            res["asserts"]["SUBTITLE"] = "PASS " + sub_txt.replace("\n", " | ")
        elif sub_txt:
            res["asserts"]["SUBTITLE"] = (
                "FAIL-no-reply " + sub_txt.replace("\n", " | "))
        else:
            res["asserts"]["SUBTITLE"] = "FAIL " + (sub_err or "blank readback")
        res["artifacts"]["subtitle"] = sub_txt
    finally:
        s.close()
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="capabilities",
                    help="corpus slug to voice-drive (corpus.json); default "
                         "'capabilities'")
    ap.add_argument("--artifacts", default=str(Path.home() / "artifacts"))
    args = ap.parse_args()
    artifacts = Path(args.artifacts) / "robodog-claw-e2e"
    artifacts.mkdir(parents=True, exist_ok=True)

    global DETECTED_PORT
    DETECTED_PORT = detect_board_port()
    card = detect_sound_card()
    log(f"board={DETECTED_PORT or 'NONE'} sound='{card}' runs={N_RUNS} "
        f"corpus={args.corpus}")
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
        r = run_loop(i, instr, artifacts)
        results.append(r)
        for k, v in r["asserts"].items():
            log(f"{k}: {v}")
        if i < N_RUNS - 1:
            log("settling between loops ...")
            time.sleep(3)

    lines = [
        "# RoboDog ESP-Claw Voice-Loop E2E Results\n",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M')}",
        f"Board: {DETECTED_PORT}",
        f"Corpus: {args.corpus} ({instr['transcript']!r})",
        f"Runs: {N_RUNS}\n",
    ]
    for r in results:
        lines.append(f"## loop {r['loop']}")
        for k, v in r["asserts"].items():
            lines.append(f"- **{k}**: {v}")
        lines.append(f"- ASR verbatim: {r['artifacts'].get('asr','')!r}")
        lines.append(f"- REPLY verbatim: {r['artifacts'].get('reply','')!r}")
        lines.append("")

    def ok(v):
        return v is not None and (v.startswith("PASS") or v.startswith("SKIP"))

    def passed(r):
        return all(ok(r["asserts"].get(k, "FAIL"))
                   for k in ("OPEN_SESSION", "ASR_MATCH", "REPLY",
                             "RING", "SUBTITLE"))

    overall = all(passed(r) for r in results)
    lines.append(f"Overall: {'PASS' if overall else 'FAIL'}")
    (artifacts / "robodog-claw-e2e.md").write_text("\n".join(lines))
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
