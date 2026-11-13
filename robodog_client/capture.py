#!/usr/bin/env python3
"""Capture the EXACT RLCD pixels from the RoboDog firmware.

Two frame formats, fetched over the USB-JTAG console and auto-detected by the
RD_SHOT header / buffer size:

- Calendar app (robodog_client/main/, u8g2): buffer reads 304x50 tiles
  (300x400 portrait panel memory) and the octet/bit order is the reference
  u8g2_st7305 driver's.
- Hermes voice app (XiaoZhi rebuilt with the RoboDog capture hook): the
  ST7305 landscape panel memory (400x300) produced by the upstream
  custom_lcd_display.cc landscape LUT — inverted here so the PNG is the
  logical screen the app drew.
- Home env client (robodog_home_client): the u8g2 buffer (304x50 tiles, same
  15200-byte size as the calendar) drawn with U8G2_R1, i.e. a 400x300 LOGICAL
  landscape screen in the panel's native portrait memory.  Its RD_SHOT header
  announces w=400 h=300, so the orientation is chosen by the header's
  w/h — never by the buffer size, which the calendar shares.

No re-rendering, no XML interpretation: the dumped bytes are literally what
the firmware pushed to the ST7305.

  robodog_client/capture.py [--port /dev/ttyACM0] [--reset] [--n 3]

Outputs (into --out, default robodog_client/shots/):
  panel_raw_portrait.png  300x400 — the panel's native pixel memory
  panel_landscape.png     400x300 — the same panel held in landscape (the
                            orientation the app draws; auto-detected so the
                            date/time text reads upright)
  <frame>.i1              raw 15200-byte buffer for reuse/record

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
        if not self._in_shot:
            m = SHOT_RE.search(line)
            if not m:
                return None
            self._in_shot = True
            self._want = int(m.group(1))
            self._w, self._h = int(m.group(2)), int(m.group(3))
            self._hex = []
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


def dihedral(img):
    """All 8 orientations: 4 rotations x mirror — one matches the panel held
    in landscape with the app's intended rotation."""
    out = {}
    for m, mir in [(0, img), (1, img.transpose(Image.FLIP_LEFT_RIGHT))]:
        for r in range(4):
            out[f"mirror{1 if m else 0}_rot{r*90}"] = mir.rotate(r * 90, expand=True)
    return out


# Vision readback — "read the screenshot so the agent can reason": the same
# self-hosted vision model hermes uses (Qwen2.5-VL at spark-eebf,
# ~/.hermes/config.yaml "vision"). Override via RD_VISION_BASE_URL /
# RD_VISION_MODEL.
VISION_BASE = os.environ.get("RD_VISION_BASE_URL", "http://spark-eebf:8000/v1")
VISION_MODEL = os.environ.get("RD_VISION_MODEL",
                              "Qwen/Qwen2.5-VL-72B-Instruct")
VISION_PROMPT = (
    "This is a monochrome RLCD family-calendar screen. Transcribe exactly:\n"
    "1) the header date/time line (or say BLANK),\n"
    "2) each visible row as 'title HH:MM',\n"
    "3) any status words like 'no wifi'/'stale'/'offline'.\n"
    "Reply as a short list.")

HOME_VISION_PROMPT = (
    "This is the monochrome RLCD screen of a home temperature/humidity panel. "
    "Transcribe exactly, in the ORIGINAL language/script:\n"
    "1) the device name at the top left and the current reading at the top "
    "right (e.g. '22.6C 55%'),\n"
    "2) the status line under it (e.g. 'updated', 'stale', 'no reply') and the "
    "sample count,\n"
    "3) the plot's axis labels (temperature on the left in C, humidity on the "
    "right in %), the unit markers at the trace ends, and the x-axis clock "
    "labels.\n"
    "Report only text that is actually drawn; do not infer a status word that "
    "is absent. If the screen is blank, say BLANK.\n"
    "Reply as a short list.")

HERMES_VISION_PROMPT = (
    "This is the monochrome RLCD screen of an AI voice-assistant device "
    "(XiaoZhi). Transcribe exactly any visible text (labels, dates, status or "
    "subtitle lines) and describe what is shown (a face/character, icons, "
    "layout). Transcribe in the ORIGINAL language/script — do not translate; "
    "keep Chinese characters exactly as displayed. If the screen is blank, "
    "say BLANK.\n"
    "Reply as a short list.")


def describe_image(path, prompt=VISION_PROMPT):
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



HOUR_RE = re.compile(r"\d{1,2}:\d{2}")
DATE_RE = re.compile(r"\d{4}[-\s/]\d{1,2}[-\s/]\d{1,2}")


def ocr_score(im):
    """3x upscale + tesseract; score an orientation by how much real calendar
    text it yields — HH:MM times dominate the native row layout."""
    im = im.resize((im.width * 3, im.height * 3), Image.Resampling.NEAREST)
    tmp = Path("/tmp/rdshot_ocr.png")
    im.convert("L").save(tmp)
    txt = subprocess.run(["tesseract", str(tmp), "-", "--psm", "6"],
                         capture_output=True, text=True).stdout
    return (len(HOUR_RE.findall(txt)), len(DATE_RE.findall(txt)), txt)


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
    ap.add_argument("--i1-format", choices=["auto", "calendar", "hermes", "home"],
                    default="auto",
                    help="frame type for --i1; 'auto' infers it from the byte "
                         "size, which cannot tell calendar (300x400 portrait) "
                         "from home (400x300 landscape) apart — they share the "
                         "15200-byte buffer")
    ap.add_argument("--describe", action="store_true",
                    help="vision-model transcription of each captured frame "
                         "(Qwen2.5-VL via the hermes vision endpoint)")
    args = ap.parse_args()

    out = Path(args.out or (Path(__file__).resolve().parent / "shots"))
    out.mkdir(parents=True, exist_ok=True)

    if args.i1:
        data = Path(args.i1).read_bytes()
        # no RD_SHOT header on an offline dump — pick the decoder by size,
        # unless the caller names the format (calendar and home share 15200 B)
        fmt = args.i1_format
        if fmt == "auto":
            fmt = ("hermes" if len(data) == HERMES_BUF_SIZE else
                   "calendar" if len(data) == BUF_SIZE else "")
        if fmt == "calendar":
            frames = [(data, BUF_SIZE, PANEL_W, PANEL_H)]
        elif fmt == "home":
            frames = [(data, BUF_SIZE, HERMES_W, HERMES_H)]
        elif fmt == "hermes":
            frames = [(data, HERMES_BUF_SIZE, HERMES_W, HERMES_H)]
        else:
            sys.exit(f"cannot infer frame type from {len(data)} bytes "
                     f"(calendar/home={BUF_SIZE} / hermes={HERMES_BUF_SIZE}); "
                     f"pass --i1-format")
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
            # The RD_SHOT header carries the LOGICAL screen size: the home
            # client draws 400x300 landscape (w>h) into the same 15200-byte
            # portrait buffer the calendar fills with 300x400.  Buffer size
            # cannot tell them apart, the header can.
            landscape = w > h
            if landscape:
                # The home firmware draws through u8g2 U8G2_R1, so the logical
                # screen is ALWAYS this dihedral of the native portrait memory.
                # Do not OCR-guess: tesseract scores rotated/mirrored gibberish
                # higher than short upright text and picked a sideways frame.
                best = ((0, 0), "mirror0_rot90", dihedral(raw)["mirror0_rot90"])
            else:
                best = None
                for name, im in dihedral(raw).items():
                    s = ocr_score(im)
                    if best is None or s[:2] > best[0][:2]:
                        best = (s, name, im)
            best[2].save(out / "panel_landscape.png")
            print(f"frame {i}: landscape view = {best[1]} "
                  f"(score={best[0][0]}/{best[0][1]}) -> "
                  f"panel_raw_portrait.png / panel_landscape.png",
                  file=sys.stderr)
            shot_for_vision = out / "panel_landscape.png"
            prompt = HOME_VISION_PROMPT if landscape else VISION_PROMPT
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
