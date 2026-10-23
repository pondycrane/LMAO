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

W_TILE = 304  # 38 tiles * 8 px
ROWS = 50  # full buffer (no paging)
PANEL_W, PANEL_H = 300, 400
BUF_SIZE = W_TILE * ROWS  # 15200

# Hermes (XiaoZhi voice) firmware frame: the ST7305 panel memory held in
# landscape, produced by the upstream custom_lcd_display.cc landscape LUT.
HERMES_W, HERMES_H = 400, 300
HERMES_BUF_SIZE = HERMES_W * HERMES_H // 8  # 15000

SHOT_RE = re.compile(r"RD_SHOT_BEGIN\s+bufsize=(\d+)\s+w=(\d+)\s+h=(\d+)(?:\s+state=(\w+))?")
HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
# Conversation-state label on hermes RD_SHOT frames (robodog_hermes_capture.patch).
# A missing/legacy header defaults to UNKNOWN so calendar frames decode unchanged.
UNKNOWN_STATE = "UNKNOWN"
# The firmware-owned RD_SHOT ``state=`` surface-tag set
# (main/boards/waveshare/esp32-s3-rlcd-4.2/robodog_statescreen.cc
# RoboDogStateScreen::StateName). Exported once here (PR review R9) so the
# hermes E2E groups/classifies frames through a single definition instead of
# re-enumerating the tokens as host string literals — a firmware-side rename /
# insertion is caught loudly instead of silently ungrouping every frame.
HERMES_STATES = frozenset({"IDLE", "LISTENING", "PROCESSING", "RESPONDING"})


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
            out[f"mirror{1 if m else 0}_rot{r * 90}"] = mir.rotate(r * 90, expand=True)
    return out


# Vision readback — "read the screenshot so the agent can reason": the same
# self-hosted vision model hermes uses (Qwen2.5-VL at spark-eebf,
# ~/.hermes/config.yaml "vision"). Override via RD_VISION_BASE_URL /
# RD_VISION_MODEL.
VISION_BASE = os.environ.get("RD_VISION_BASE_URL", "http://spark-eebf:8000/v1")
VISION_MODEL = os.environ.get("RD_VISION_MODEL", "Qwen/Qwen2.5-VL-72B-Instruct")
VISION_PROMPT = (
    "This is a monochrome RLCD family-calendar screen. Transcribe exactly:\n"
    "1) the header date/time line (or say BLANK),\n"
    "2) each visible row as 'title HH:MM',\n"
    "3) any status words like 'no wifi'/'stale'/'offline'.\n"
    "Reply as a short list."
)

HERMES_VISION_PROMPT = (
    "This is the monochrome RLCD screen of an AI voice-assistant device "
    "(XiaoZhi). Transcribe exactly any visible text (labels, dates, status or "
    "subtitle lines) and describe what is shown (a face/character, icons, "
    "layout). If the screen is blank, say BLANK.\n"
    "Reply as a short list."
)

# Cursor-aware readback for the 3-state conversation canvas: the E2E needs to
# know exactly what follows the last word of the transcript so it can prove the
# LISTENING block cursor / PROCESSING ellipsis / RESPONDING underscore.
HERMES_CURSOR_PROMPT = (
    "This is the monochrome RLCD screen of an AI voice-assistant device "
    "(XiaoZhi) during a conversation.\n"
    "1) Transcribe every text line exactly (there are at most two).\n"
    "2) After the last word of the last line, describe exactly what follows: "
    "a solid block, an ellipsis '...', an underscore '_', or nothing.\n"
    "3) State whether a solid block appears after the first line's text.\n"
    "If the screen is blank, say BLANK. Reply as a short list."
)


def describe_image(path, prompt=VISION_PROMPT):
    """Vision-model transcription of a captured frame (OpenAI-compatible)."""
    img = base64.b64encode(Path(path).read_bytes()).decode()
    body = {
        "model": VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        "max_tokens": 400,
    }
    req = urllib.request.Request(
        f"{VISION_BASE}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
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
    txt = subprocess.run(
        ["tesseract", str(tmp), "-", "--psm", "6"], capture_output=True, text=True
    ).stdout
    return (len(HOUR_RE.findall(txt)), len(DATE_RE.findall(txt)), txt)


def read_frames(port, n=1, timeout=30):
    """Capture n RD_SHOT frames from the serial port.

    Each frame is ``(data, expect, w, h, state, t)`` where ``state`` is the
    conversation-state label from the RD_SHOT header (``UNKNOWN_STATE`` for
    calendar/legacy frames — backward compatible; callers may ignore it) and
    ``t`` is the epoch-second stamp taken the MOMENT that frame's ``RD_SHOT_END``
    was seen on the console (PR review R7: per-frame dump time, not batch end).
    """
    frames = []
    s = serial.Serial(port, 115200, timeout=1)
    try:
        deadline = time.time() + timeout
        in_shot = False
        want = 0
        frame_state = UNKNOWN_STATE
        hex_acc = []
        while len(frames) < n and time.time() < deadline:
            # 16 KiB chunks keep up with the ~11.5 KB/s RD_SHOT dump rate
            # (a 4096-byte read onto the USB-JTAG CDC buffer overran and
            # shortened frames during the boot-log + dump burst).
            chunk = s.read(16384)
            if not chunk:
                continue
            for line in chunk.split(b"\n"):
                ln = line.decode("utf-8", "replace").strip()
                if not in_shot:
                    m = SHOT_RE.search(ln)
                    if m:
                        in_shot = True
                        want = int(m.group(1))
                        w = int(m.group(2))
                        h = int(m.group(3))
                        frame_state = (m.group(4) or UNKNOWN_STATE).upper()
                        hex_acc = []
                else:
                    if "RD_SHOT_END" in ln:
                        body = "".join(hex_acc)
                        data = bytes.fromhex(body)
                        # Stamp at the exact moment this frame's dump completed
                        # (R7): a batch-end stamp makes every sidecar share one
                        # time and the journal-window corroboration predict
                        # IDLE for every conversation frame.
                        frames.append((data, want, w, h, frame_state, time.time()))
                        in_shot = False
                        print(
                            f"captured frame {len(frames)} "
                            f"({len(data)} bytes, {w}x{h}, state={frame_state})",
                            file=sys.stderr,
                        )
                    elif HEX_RE.match(ln):
                        hex_acc.append(ln)
    finally:
        s.close()
    return frames


def reset_board(port):
    print(f"resetting {port} (esptool default_reset/hard_reset) ...", file=sys.stderr)
    subprocess.run(
        [
            "python3",
            "-m",
            "esptool",
            "--chip",
            "esp32s3",
            "-p",
            port,
            "--baud",
            "460800",
            "--before",
            "default_reset",
            "--after",
            "hard_reset",
            "flash_id",
        ],
        check=False,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--out", default=None, help="output dir (default robodog_client/shots)")
    ap.add_argument("--n", type=int, default=1, help="frames to capture")
    ap.add_argument("--reset", action="store_true", help="reboot the app first (fresh frame dumps)")
    ap.add_argument(
        "--listen",
        action="store_true",
        help="write the RD_LISTEN software-button trigger over the "
        "console before capturing (opens the voice session)",
    )
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument(
        "--i1", metavar="FILE", default=None, help="decode an already-captured .i1 dump (no device)"
    )
    ap.add_argument(
        "--describe",
        action="store_true",
        help="vision-model transcription of each captured frame "
        "(Qwen2.5-VL via the hermes vision endpoint)",
    )
    args = ap.parse_args()

    out = Path(args.out or (Path(__file__).resolve().parent / "shots"))
    out.mkdir(parents=True, exist_ok=True)

    if args.i1:
        data = Path(args.i1).read_bytes()
        # no RD_SHOT header on an offline dump — pick the decoder by size
        if len(data) == BUF_SIZE:
            frames = [(data, BUF_SIZE, PANEL_W, PANEL_H, UNKNOWN_STATE, time.time())]
        elif len(data) == HERMES_BUF_SIZE:
            frames = [(data, HERMES_BUF_SIZE, HERMES_W, HERMES_H, UNKNOWN_STATE, time.time())]
        else:
            sys.exit(
                f"cannot infer frame type from {len(data)} bytes "
                f"(calendar={BUF_SIZE} / hermes={HERMES_BUF_SIZE})"
            )
    else:
        if args.reset:
            reset_board(args.port)
            time.sleep(1)
        if args.listen:
            print(f"sending RD_LISTEN to {args.port} (software button) ...", file=sys.stderr)
            s = serial.Serial(args.port, 115200, timeout=1)
            try:
                # USB-Serial/JTAG arms RX only after the CDC port is open+settled;
                # writing immediately races enumeration and the byte is lost.
                time.sleep(1.0)
                s.reset_input_buffer()
                s.write(b"RD_LISTEN\r\n")
                s.flush()
                time.sleep(0.2)
            finally:
                s.close()
            time.sleep(1)
        frames = read_frames(args.port, n=args.n, timeout=args.timeout)
        if not frames:
            sys.exit("no RD_SHOT frames captured — is robodog/hermes firmware flashed?")

    for i, (data, expect, w, h, frame_state, _t) in enumerate(frames):
        if len(data) != expect:
            print(f"frame {i}: expected {expect} bytes, got {len(data)}", file=sys.stderr)
            continue
        (out / f"frame{i}.i1").write_bytes(data)

        hermes = expect == HERMES_BUF_SIZE
        if hermes:
            # ST7305 landscape panel memory already reads as the logical screen.
            raw = decode_hermes(data)
            raw.save(out / "panel_landscape.png")
            print(f"frame {i}: hermes landscape {w}x{h} -> panel_landscape.png", file=sys.stderr)
            shot_for_vision = out / "panel_landscape.png"
            prompt = HERMES_VISION_PROMPT
        else:
            raw = decode_raw(data)
            raw.save(out / "panel_raw_portrait.png")
            # pick the orientation where the date/time text reads upright
            best = None
            for name, im in dihedral(raw).items():
                hhmm, dates, _ = ocr_score(im)
                if best is None or (hhmm, dates) > best[0]:
                    best = ((hhmm, dates), name, im)
            best[2].save(out / "panel_landscape.png")
            print(
                f"frame {i}: landscape view = {best[1]} "
                f"(OCR hh:mm={best[0][0]}, dates={best[0][1]}) -> "
                f"panel_raw_portrait.png / panel_landscape.png",
                file=sys.stderr,
            )
            shot_for_vision = out / "panel_landscape.png"
            prompt = VISION_PROMPT
        if args.describe:
            print(f"frame {i}: vision readback (state={frame_state}) ...", file=sys.stderr)
            try:
                txt = describe_image(shot_for_vision, prompt=prompt)
                (out / f"frame{i}_analysis.txt").write_text(txt)
                print(f"--- frame {i} vision readback ---\n{txt}")
            except Exception as e:  # pragma: no cover — network/model path
                print(f"frame {i}: vision readback failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
