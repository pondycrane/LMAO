#!/usr/bin/env python3
"""Capture the EXACT RLCD pixels from the RoboDog firmware.

Two frame formats, fetched over the USB-JTAG console and auto-detected by the
RD_SHOT header / buffer size:

- u8g2 firmwares (calendar `robodog_client`, home `robodog_home_client`):
  the SAME 304x50-tile (15200-byte) u8g2 buffer, both drawn through
  `U8G2_R1` over the ST7305's native 300x400 portrait memory.  The logical
  screen is therefore 400x300 landscape in BOTH, and the same single rotation
  (`raw.rotate(90)`) recovers it — the buffer size and the RD_SHOT header
  (both announce w=400 h=300) cannot tell the two firmwares apart, and they
  do not need to: the decode is identical, only the vision prompt differs
  (see PANEL_VISION_PROMPT).
- Hermes voice app (XiaoZhi rebuilt with the RoboDog capture hook): the
  ST7305 landscape panel memory (400x300) produced by the upstream
  custom_lcd_display.cc landscape LUT — inverted here so the PNG is the
  logical screen the app drew.  Its 15000-byte frame is unambiguous.

No re-rendering, no XML interpretation: the dumped bytes are literally what
the firmware pushed to the ST7305.

  robodog_client/capture.py [--port /dev/ttyACM0] [--reset] [--n 3]

Outputs (into --out, default robodog_client/shots/):
  panel_raw_portrait.png  300x400 — the panel's native pixel memory
  panel_landscape.png     400x300 — the logical screen the app drew (u8g2
                            frames are always U8G2_R1, so this is a fixed
                            rotation, not an OCR guess)
  <frame>.i1              raw buffer (15200 B u8g2 / 15000 B hermes) for reuse

--reset re-runs the app into a fresh boot (esptool default_reset/hard_reset —
the same sequence flash.sh uses) so the firmware emits its capture frames
again.
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import serial

from PIL import Image

W_TILE = 304     # 38 tiles * 8 px
ROWS = 50        # full buffer (no paging)
PANEL_W, PANEL_H = 300, 400
BUF_SIZE = W_TILE * ROWS   # 15200

# Hermes (XiaoZhi voice) firmware frame: the ST7305 panel memory held in
# landscape, produced by the upstream custom_lcd_display.cc landscape LUT.
HERMES_W, HERMES_H = 400, 300
HERMES_BUF_SIZE = HERMES_W * HERMES_H // 8    # 15000

SHOT_RE = re.compile(r"RD_SHOT_BEGIN\s+bufsize=(\d+)\s+w=(\d+)\s+h=(\d+)")
HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


class ShotAssembler:
    """Incremental RD_SHOT parser: feed console lines, get frames back.

    ONE implementation of the frame protocol.  `capture.py`'s serial reader and
    the ESP-Claw E2E (which scans a drained console) previously each had their
    own copy of the BEGIN/hex/END state machine; they had already drifted (only
    the claw copy handled truncated frames).  With `validate=True` a truncated
    dump is discarded and scanning continues for the next complete frame (the
    forced `rdshot` dump races the firmware's self-tick dump, so the first
    frame after a trigger is often short).
    """

    def __init__(self, validate=True):
        self.validate = validate
        self.dropped = 0
        self._in_shot = False
        self._want = 0
        self._w = self._h = 0
        self._hex = []

    def feed(self, line):
        """Consume one console line; return (data, bufsize, w, h) on a frame.

        `bufsize` is the size the header announced, so a caller can tell a
        complete dump from a truncated one (with validate=False)."""
        # A BEGIN while a dump is open means its END was lost (the serial read
        # raced the firmware's self-tick dump).  The partial frame is unusable
        # either way, so resync on the new header instead of swallowing it and
        # losing the following frame's hex too.
        m = SHOT_RE.search(line)
        if m:
            if self._in_shot:
                self.dropped += 1
            self._in_shot = True
            self._want = int(m.group(1))
            self._w, self._h = int(m.group(2)), int(m.group(3))
            self._hex = []
            return None
        if not self._in_shot:
            return None
        if "RD_SHOT_END" in line:
            data = bytes.fromhex("".join(self._hex))
            self._in_shot = False
            self._hex = []
            if self.validate and len(data) != self._want:
                self.dropped += 1
                return None      # truncated: keep scanning
            return data, self._want, self._w, self._h
        if HEX_RE.match(line):
            self._hex.append(line)
        return None

    @property
    def in_shot(self):
        return self._in_shot


def decode_hermes(buf):
    """XiaoZhi DispBuffer -> the 400x300 logical screen (bit=0 black, 1 white).

    Inverse of InitLandscapeLUT() in main/boards/waveshare/esp32-s3-rlcd-4.2/
    custom_lcd_display.cc: for each logical (x,y) the panel bit sits at
    index = (x>>1)*H4 + ((H-1-y)>>2), bit = 7 - (((H-1-y)&3)<<1 | (x&1)),
    where bit==1 is white paper (ColorWhite=0xff sets the bit).
    """
    img = Image.new("L", (HERMES_W, HERMES_H), 255)
    px = img.load()
    h4 = HERMES_H >> 2
    for y in range(HERMES_H):
        inv_y = HERMES_H - 1 - y
        block_y = inv_y >> 2
        local_y = inv_y & 3
        for x in range(HERMES_W):
            index = (x >> 1) * h4 + block_y
            bit = 7 - ((local_y << 1) | (x & 1))
            if not (buf[index] >> bit) & 1:
                px[x, y] = 0
    return img


def decode_raw(buf):
    """u8g2 buffer -> 300x400 portrait greyscale (bit=white paper, as drawn)."""
    img = Image.new("L", (PANEL_W, PANEL_H), 255)
    px = img.load()
    for y in range(PANEL_H):
        base = (y >> 3) * W_TILE
        for x in range(PANEL_W):
            if not (buf[base + x] >> (y & 7)) & 1:
                px[x, y] = 0
    return img


LANDSCAPE_W, LANDSCAPE_H = PANEL_H, PANEL_W   # 400x300: the u8g2 logical screen


def landscape_view(raw):
    """u8g2 U8G2_R1 -> the 400x300 logical screen the firmware drew.

    R1 maps logical (x, y) to native (H_native-1-y, x) (`u8g2_draw_l90_r1`),
    so the inverse is a single 90 deg counter-clockwise rotation of the native
    portrait image.  Both u8g2 firmwares (calendar and home) use R1, so this is
    fixed — never an OCR guess over the dihedral group (tesseract scored
    rotated gibberish higher than short upright text and picked a sideways
    frame).
    """
    return raw.rotate(90, expand=True)


# Vision readback — "read the screenshot so the agent can reason": the same
# self-hosted vision model hermes uses (Qwen2.5-VL at spark-eebf,
# ~/.hermes/config.yaml "vision"). Override via RD_VISION_BASE_URL /
# RD_VISION_MODEL.
VISION_BASE = os.environ.get("RD_VISION_BASE_URL", "http://spark-eebf:8000/v1")
VISION_MODEL = os.environ.get("RD_VISION_MODEL",
                              "Qwen/Qwen2.5-VL-72B-Instruct")
# The u8g2 frames (calendar and home) share a buffer size and a decode, so one
# prompt must serve both: transcribe EVERY drawn line verbatim and let the
# caller's assertions pick what matters (clock + rows for the calendar, name +
# reading + status for the env panel).
PANEL_VISION_PROMPT = (
    "This is a monochrome reflective LCD screen (400x300, one ink). Transcribe "
    "exactly, in the ORIGINAL language/script, every line of text actually "
    "drawn, top to bottom — e.g. a device name, a date/time or clock label, "
    "event rows, a temperature/humidity reading with its unit, a status word "
    "('updated', 'stale', 'no wifi', 'no reply'), a sample count, or plot axis "
    "labels. Do not translate and do not infer a word that is absent; if the "
    "screen has no text, say BLANK.\n"
    "Reply as a short list.")

HERMES_VISION_PROMPT = (
    "This is the monochrome RLCD screen of an AI voice-assistant device "
    "(XiaoZhi). Transcribe exactly any visible text (labels, dates, status or "
    "subtitle lines) and describe what is shown (a face/character, icons, "
    "layout). Transcribe in the ORIGINAL language/script — do not translate; "
    "keep Chinese characters exactly as displayed. If the screen is blank, "
    "say BLANK.\n"
    "Reply as a short list.")


def describe_image(path, prompt=PANEL_VISION_PROMPT):
    """Vision-model transcription of a captured frame (OpenAI-compatible)."""
    img = base64.b64encode(Path(path).read_bytes()).decode()
    body = {
        "model": VISION_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{img}"}},
            {"type": "text", "text": prompt},
        ]}],
        "max_tokens": 400,
    }
    req = urllib.request.Request(
        f"{VISION_BASE}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        out = json.load(resp)
    return out["choices"][0]["message"]["content"]



def wait_for_port(port, timeout=15.0):
    """Block until `port` exists again after a reset.

    esptool's hard reset re-enumerates the USB-Serial/JTAG interface, so the
    node disappears for a moment; opening it immediately races the kernel and
    yields ENOENT.  The same re-enumeration can also invalidate an already-open
    handle mid-capture (the CDC interface is torn down under the reader)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if Path(port).exists():
            return True
        time.sleep(0.2)
    return Path(port).exists()


def read_frames(port, n=1, timeout=30):
    frames = []
    wait_for_port(port)
    s = serial.Serial(port, 115200, timeout=1)
    try:
        deadline = time.time() + timeout
        # validate=True: a truncated dump is discarded and scanning continues
        # for the next complete frame.  The 16 KiB chunked read races the
        # boot-log + dump burst, so the first frame after a reset is regularly
        # short (observed 15168..15183 of 15200 B); accepting it produced a
        # frame that failed the length check downstream and left a capture job
        # with no decoded panel at all.
        assembler = ShotAssembler(validate=True)
        while len(frames) < n and time.time() < deadline:
            # 16 KiB chunks keep up with the ~11.5 KB/s RD_SHOT dump rate
            # (a 4096-byte read onto the USB-JTAG CDC buffer overran and
            # shortened frames during the boot-log + dump burst).
            try:
                chunk = s.read(16384)
            except serial.SerialException:
                # The device re-enumerated (USB-Serial/JTAG tears the CDC
                # interface down on reset/reboot).  Keep the frames already
                # parsed instead of aborting the whole run — a reset-triggered
                # capture ALWAYS races this, and the boot frames are the ones
                # the caller wants.
                print(f"serial interface dropped after {len(frames)} frame(s) "
                      f"(device re-enumerated) — using what was captured",
                      file=sys.stderr)
                break
            if not chunk:
                continue
            for line in chunk.split(b"\n"):
                ln = line.decode("utf-8", "replace").strip()
                got = assembler.feed(ln)
                if got is None:
                    continue
                data, want, w, h = got
                frames.append((data, want, w, h))
                print(f"captured frame {len(frames)} "
                      f"({len(data)} bytes, {w}x{h})",
                      file=sys.stderr)
        if assembler.dropped:
            print(f"discarded {assembler.dropped} truncated frame(s)",
                  file=sys.stderr)
    finally:
        s.close()
    return frames


def reset_board(port):
    print(f"resetting {port} (esptool default_reset/hard_reset) ...",
          file=sys.stderr)
    subprocess.run(
        ["python3", "-m", "esptool", "--chip", "esp32s3", "-p", port,
         "--baud", "460800", "--before", "default_reset",
         "--after", "hard_reset", "flash_id"],
        check=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--out", default=None,
                    help="output dir (default robodog_client/shots)")
    ap.add_argument("--n", type=int, default=1, help="frames to capture")
    ap.add_argument("--reset", action="store_true",
                    help="reboot the app first (fresh frame dumps)")
    ap.add_argument("--listen", action="store_true",
                    help="write the RD_LISTEN software-button trigger over the "
                         "console before capturing (opens the voice session)")
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--i1", metavar="FILE", default=None,
                    help="decode an already-captured .i1 dump (no device)")
    ap.add_argument("--describe", action="store_true",
                    help="vision-model transcription of each captured frame "
                         "(Qwen2.5-VL via the hermes vision endpoint)")
    args = ap.parse_args()

    out = Path(args.out or (Path(__file__).resolve().parent / "shots"))
    out.mkdir(parents=True, exist_ok=True)

    if args.i1:
        data = Path(args.i1).read_bytes()
        # No RD_SHOT header on an offline dump; the buffer size is unambiguous
        # (15000 B hermes landscape LUT, 15200 B u8g2 tiles) and both u8g2
        # firmwares share the 15200-byte size AND the R1 decode.
        if len(data) == HERMES_BUF_SIZE:
            frames = [(data, HERMES_BUF_SIZE, HERMES_W, HERMES_H)]
        elif len(data) == BUF_SIZE:
            frames = [(data, BUF_SIZE, LANDSCAPE_W, LANDSCAPE_H)]
        else:
            sys.exit(f"cannot decode {len(data)} bytes "
                     f"(u8g2={BUF_SIZE} / hermes={HERMES_BUF_SIZE})")
    else:
        if args.reset:
            reset_board(args.port)
            time.sleep(1)
        if args.listen:
            print(f"sending RD_LISTEN to {args.port} (software button) ...",
                  file=sys.stderr)
            from e2e_rig import arm_and_send  # noqa: PLC0415

            arm_and_send(args.port, "RD_LISTEN", settle=1.0, after=0.2)
            time.sleep(1)
        frames = read_frames(args.port, n=args.n, timeout=args.timeout)
        if not frames:
            sys.exit("no RD_SHOT frames captured — is robodog/hermes "
                     "firmware flashed?")

    for i, (data, expect, w, h) in enumerate(frames):
        if len(data) != expect:
            print(f"frame {i}: expected {expect} bytes, got {len(data)}",
                  file=sys.stderr)
            continue
        (out / f"frame{i}.i1").write_bytes(data)

        hermes = (expect == HERMES_BUF_SIZE)
        if hermes:
            # ST7305 landscape panel memory already reads as the logical screen.
            raw = decode_hermes(data)
            raw.save(out / "panel_landscape.png")
            print(f"frame {i}: hermes landscape {w}x{h} -> "
                  f"panel_landscape.png", file=sys.stderr)
            shot_for_vision = out / "panel_landscape.png"
            prompt = HERMES_VISION_PROMPT
        else:
            raw = decode_raw(data)
            raw.save(out / "panel_raw_portrait.png")
            # u8g2 firmwares (calendar and home) both draw through U8G2_R1 into
            # the native 300x400 portrait memory, so the logical 400x300 screen
            # is ALWAYS this rotation of it.  The RD_SHOT header (w=400 h=300)
            # and the buffer size (15200 B) are identical for both, so neither
            # identifies the firmware — and neither needs to.
            best = landscape_view(raw)
            best.save(out / "panel_landscape.png")
            print(f"frame {i}: landscape view = rot90 (u8g2 R1) -> "
                  f"panel_raw_portrait.png / panel_landscape.png",
                  file=sys.stderr)
            shot_for_vision = out / "panel_landscape.png"
            prompt = PANEL_VISION_PROMPT
        if args.describe:
            print(f"frame {i}: vision readback ...", file=sys.stderr)
            try:
                txt = describe_image(shot_for_vision, prompt=prompt)
                (out / f"frame{i}_analysis.txt").write_text(txt)
                print(f"--- frame {i} vision readback ---\n{txt}")
            except Exception as e:   # pragma: no cover — network/model path
                print(f"frame {i}: vision readback failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
