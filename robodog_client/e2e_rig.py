"""Shared host-side scaffolding for the RoboDog hardware E2E gates.

`voice/hermes_voice_e2e.py` and `esp-claw/robodog_claw_e2e.py` are two
different gates (bridge journal vs. device console) on the SAME board and the
SAME test rig.  Their preflight, ALSA audio drive/capture, board-port detection
and re-ASR helpers had been copy-pasted between them — and had already
diverged (two different `detect_board_port` implementations, a claw-local
`trim_loudest_window` re-implementing `e2e_readout.energy_burst_window`, and a
raw Whisper POST duplicating the bridge's call).  Everything rig-level lives
here once; each gate keeps only its own assertion logic.

Pure stdlib apart from `pyserial`, which is imported lazily so importing this
module on a host without it (e.g. a Bazel py_test) still works.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import wave
from pathlib import Path

# e2e_readout lives in robodog_client/voice/ (the hermes gate's module).
_VOICE_DIR = str(Path(__file__).resolve().parent / "voice")
if _VOICE_DIR not in sys.path:
    sys.path.insert(0, _VOICE_DIR)

from e2e_readout import energy_burst_window  # noqa: E402

BOARD_VID, BOARD_PID = 0x303A, 0x1001

# The ALSA nodes belong to the `audio` group; `sg audio -c '<cmd>'` runs the
# audio command with that group without needing a fresh login.  An operator
# already in the group (or root) sets E2E_AUDIO_SG="" to emit the bare command.
SILENCE = os.environ.get("E2E_AUDIO_SG", "sg audio -c")

# A 16 KiB read keeps up with the RD_SHOT dump rate; the USB-JTAG CDC buffer
# overran and shortened frames with 4096-byte reads during a boot-log burst.
CONSOLE_READ_CHUNK = 16384


def log(msg):
    print(f"[e2e] {time.strftime('%H:%M:%S')} {msg}", flush=True)


def shell_run(cmd, timeout=120):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                          timeout=timeout)


# ---------------------------------------------------------------------------
# Rig preflight
# ---------------------------------------------------------------------------
def detect_board_port():
    """Board serial port: ROBODOG_PORT, else the 0x303A:0x1001 USB-JTAG, else a
    by-id USB-Serial/JTAG node, else the first /dev/ttyACM*.

    One implementation for both gates: the hermes copy only matched the USB
    VID/PID (which fails on hosts whose pyserial can't enumerate the JTAG
    interface) and the claw copy only globbed device nodes (which can pick a
    different board).  Trying them in order covers both situations.
    """
    port = os.environ.get("ROBODOG_PORT", "")
    if port == "AUTO":
        port = ""
    if port:
        return port

    try:
        import serial.tools.list_ports  # noqa: PLC0415

        for p in serial.tools.list_ports.comports():
            if p.vid == BOARD_VID and p.pid == BOARD_PID:
                return p.device
    except ImportError:
        pass

    cands = []
    for dev in Path("/dev/serial/by-id").glob("*"):
        if "USB-Serial" in dev.name or "JTAG" in dev.name:
            cands.append(os.path.realpath(dev))
    for cand in sorted(set(cands)):
        if Path(cand).exists():
            return cand

    for cand in sorted(Path("/dev").glob("ttyACM*")):
        if cand.exists():
            return str(cand)
    return None


def detect_sound_card(default="Device"):
    """Configured ALSA card id, else the first USB audio card."""
    for line in Path("/proc/asound/cards").read_text().splitlines():
        if "USB-Audio" in line and "[" in line:
            card = line.split("[", 1)[1].split("]", 1)[0].strip()
            if card:
                return card
    try:
        out = subprocess.run(["aplay", "-L"], capture_output=True, text=True)
        for line in out.stdout.splitlines():
            line = line.strip()
            if line and "default" not in line and line != "null":
                return line
    except Exception:  # noqa: BLE001 — aplay absent is not fatal
        pass
    return default


# ---------------------------------------------------------------------------
# ALSA audio drive + capture (test speaker / test mic)
# ---------------------------------------------------------------------------
def audio_cmd(core, sg=None):
    """Wrap a raw ffmpeg command for the test rig.

    When E2E_AUDIO_SG is empty the operator runs as root / a group member
    directly — then emit the BARE command: prefixing an empty string would
    leave ` 'ffmpeg ...' `, which the shell reads as a single space-containing
    COMMAND NAME and fails with 127, silently killing playback/capture.
    """
    sg = SILENCE if sg is None else sg
    return f"{sg} '{core}'" if sg else core


def play_wav(path, device, volume=0.7, loop=1, channels=1, rate=16000,
             sg=None):
    """Play a WAV into the board mic through the test speaker.

    Playback must be moderate: >~0.8 volume saturates the board mic so its VAD
    never sees silence and the ASR buffer blows up (measured live).  `loop`
    repeats the clip back-to-back (the board's mic sits far from the test
    speaker, so a marginal coupling needs a repeatable signal).
    """
    loop_arg = f"-stream_loop {loop - 1} " if loop > 1 else ""
    rate_arg = f"-ar {rate} " if rate else ""
    core = (f"ffmpeg -hide_banner -loglevel error -re {loop_arg}-i {path} "
            f"-af volume={volume:.2f} -ac {channels} {rate_arg}-f alsa "
            f"{device} -y")
    return shell_run(audio_cmd(core, sg), timeout=120)


def capture_mic(path, device, seconds, channels=1, rate=16000, sg=None):
    rate_arg = f"-ar {rate} " if rate else ""
    core = (f"ffmpeg -hide_banner -loglevel error -f alsa -i {device} "
            f"-t {seconds:.1f} -ac {channels} {rate_arg}{path} -y")
    return shell_run(audio_cmd(core, sg), timeout=int(seconds) + 30)


def trim_wav(path, start, end, out, rate=16000, sg=None):
    core = (f"ffmpeg -hide_banner -loglevel error -i {path} "
            f"-ss {start:.2f} -t {max(0.1, end - start):.2f} -ac 1 -ar {rate} "
            f"{out} -y")
    return shell_run(audio_cmd(core, sg), timeout=30)


def trim_loudest_window(path, out, seconds):
    """Cut the loudest ~`seconds` window out of a mic capture (the TTS reply
    burst), so re-ASR sees the reply and not room tone.  Returns the start
    offset in seconds, or None when the capture is unreadable.

    Uses `e2e_readout.energy_burst_window` (the hermes RING path's helper)
    instead of the claw gate's private re-implementation of the same idea.
    """
    start, end = energy_burst_window(path, seconds)
    trim_wav(path, start, end, out)
    return start


def wave_duration(path):
    """Seconds of audio in a PCM WAV (0.0 when unreadable)."""
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:  # noqa: BLE001
        return 0.0


# ---------------------------------------------------------------------------
# Re-ASR of the board's spoken reply (OpenAI-compatible Whisper endpoint)
# ---------------------------------------------------------------------------
def remote_asr_omni(wav_path, url, model, timeout=120):
    """Transcribe a WAV with the OMNI model's chat-completions `input_audio`
    shape (the realtime lane's ASR backend).

    The deployed Qwen3-Omni realtime lane never emits the transcription event,
    and Whisper is retired on that lane — this is the host-side replacement for
    `remote_asr()`'s Whisper multipart POST, used by the RING / FULL_READOUT
    acoustic legs.  `modalities:["text"]` keeps the response tiny (without it
    the model also returns ~50 KB of audio)."""
    wav = base64.b64encode(Path(wav_path).read_bytes()).decode()
    body = {
        "model": model,
        "modalities": ["text"],
        "messages": [{
            "role": "user",
            "content": [
                {"type": "input_audio",
                 "input_audio": {"data": wav, "format": "wav"}},
                {"type": "text",
                 "text": "Transcribe the audio verbatim. "
                         "Output only the transcript."},
            ],
        }],
    }
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    return data["choices"][0]["message"]["content"].strip()


def remote_asr(wav_path, url, model="whisper-1", timeout=300):
    boundary = "--lmao-ring-" + os.urandom(8).hex()
    wav = Path(wav_path).read_bytes()
    fields = [
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="model"\r\n\r\n'
        f"{model}\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; '
        'filename="ring.wav"\r\n'
        "Content-Type: audio/wav\r\n\r\n"
    ]
    body = ("".join(fields)).encode() + wav + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)["text"]


# ---------------------------------------------------------------------------
# Device console tail
# ---------------------------------------------------------------------------
class ConsoleTail:
    """Owns the device console and drains it CONTINUOUSLY.

    Reading only while a wait runs loses lines whenever the host blocks
    elsewhere: playback (~10 s) and a remote re-ASR (tens of seconds) leave the
    USB-Serial/JTAG buffer unread while the device keeps printing (the RD_SHOT
    self-tick alone is ~7 KB/s), so the next session's lines are dropped — the
    gate then reports a false "session ended before ASR" on a board that
    actually worked.  A background thread therefore reads the port for the
    whole session and every scanner is a cursor over the accumulated lines
    (start = an index, not a byte offset), so no consumer can consume or
    discard another's lines.

    `on_line` runs for every drained line (the claw gate feeds its reset/panic
    sentinel scan through it)."""

    def __init__(self, port, baud=115200, on_line=None):
        import serial  # noqa: PLC0415 — lazy: not needed on non-rig hosts

        self.s = serial.Serial(port, baud, timeout=0.2)
        time.sleep(1.0)  # let USB-Serial/JTAG enumeration settle
        self.s.reset_input_buffer()
        self._cond = threading.Condition()
        self._lines = []
        self._closed = False
        self._on_line = on_line
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self):
        acc = b""
        while not self._closed:
            try:
                data = self.s.read(CONSOLE_READ_CHUNK)
            except Exception:  # noqa: BLE001 — port vanished: stop draining
                break
            if not data:
                continue
            acc += data
            while b"\n" in acc:
                raw, acc = acc.split(b"\n", 1)
                ln = raw.decode("utf-8", "replace").strip()
                if self._on_line is not None:
                    self._on_line(ln)
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


def arm_and_send(port, line, settle=1.0, after=0.2, baud=115200):
    """Open the USB-Serial/JTAG console, arm RX, write one command line, close.

    The JTAG CDC interface only arms RX after the port has been open and
    settled; writing immediately races enumeration and the byte is lost.  This
    was the third copy of the same dance (hermes `send_rd_listen`, capture.py's
    `--listen`, and the claw console writer); one helper keeps the settle
    policy in one place."""
    import serial  # noqa: PLC0415 — lazy: not needed on non-rig hosts

    s = serial.Serial(port, baud, timeout=1)
    try:
        time.sleep(settle)
        s.reset_input_buffer()
        s.write(f"{line}\r\n".encode())
        s.flush()
        time.sleep(after)
    finally:
        s.close()


def wait_for_log(tail, patterns, timeout, label="", start=None):
    """Scan the drained console from `start` for any of `patterns` (regex,
    compiled or string).  Returns (matched_line_or_None, cursor)."""
    compiled = [re.compile(p) if isinstance(p, str) else p for p in patterns]
    deadline = time.time() + timeout
    pos = tail.mark() if start is None else start
    while True:
        lines, pos = tail.take(pos, max(0.0, deadline - time.time()))
        for ln in lines:
            for pat in compiled:
                if pat.search(ln):
                    log(f"{label or 'match'}: {ln[:160]}")
                    return ln, pos
        if time.time() >= deadline:
            return None, pos


def main():  # pragma: no cover - manual rig probe
    port = detect_board_port()
    print(f"board={port} card={detect_sound_card()}")
    if not port:
        print("no board (0x303A:0x1001) attached", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
