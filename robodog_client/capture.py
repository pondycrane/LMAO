#!/usr/bin/env python3
"""Capture the EXACT RLCD pixels from the RoboDog u8g2 native firmware.

Reads the u8g2 full-frame buffer that the firmware streams over the USB-JTAG
console (built with RD_SHOT_CAPTURE=1) and reconstructs what is really on the
panel — no re-rendering, no XML interpretation. The dumped bytes are literally
the ST7305's memory content (304x50 tiles = 300x400 portrait panel).

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

SHOT_RE = re.compile(r"RD_SHOT_BEGIN\s+bufsize=(\d+)\s+w=(\d+)\s+h=(\d+)")
HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


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


def describe_image(path):
    """Vision-model transcription of a captured frame (OpenAI-compatible)."""
    img = base64.b64encode(Path(path).read_bytes()).decode()
    body = {
        "model": VISION_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{img}"}},
            {"type": "text", "text": VISION_PROMPT},
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


def read_frames(port, n=1, timeout=30):
    frames = []
    s = serial.Serial(port, 115200, timeout=1)
    try:
        deadline = time.time() + timeout
        in_shot = False
        want = 0
        hex_acc = []
        while len(frames) < n and time.time() < deadline:
            chunk = s.read(4096)
            if not chunk:
                continue
            for line in chunk.split(b"\n"):
                ln = line.decode("utf-8", "replace").strip()
                if not in_shot:
                    m = SHOT_RE.search(ln)
                    if m:
                        in_shot = True
                        want = int(m.group(1))
                        hex_acc = []
                else:
                    if "RD_SHOT_END" in ln:
                        body = "".join(hex_acc)
                        data = bytes.fromhex(body)
                        frames.append((data, want))
                        in_shot = False
                        print(f"captured frame {len(frames)} ({len(data)} bytes",
                              file=sys.stderr)
                    elif HEX_RE.match(ln):
                        hex_acc.append(ln)
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
        frames = [(Path(args.i1).read_bytes(), BUF_SIZE)]
    else:
        if args.reset:
            reset_board(args.port)
            time.sleep(1)
        frames = read_frames(args.port, n=args.n, timeout=args.timeout)
        if not frames:
            sys.exit("no RD_SHOT frames captured — is the capture build flashed?")

    for i, (data, expect) in enumerate(frames):
        if len(data) != expect:
            print(f"frame {i}: expected {expect} bytes, got {len(data)}",
                  file=sys.stderr)
            continue
        (out / f"frame{i}.i1").write_bytes(data)

        raw = decode_raw(data)
        raw.save(out / "panel_raw_portrait.png")

        # pick the orientation where the date/time text reads upright
        best = None
        for name, im in dihedral(raw).items():
            hhmm, dates, _ = ocr_score(im)
            if best is None or (hhmm, dates) > best[0]:
                best = ((hhmm, dates), name, im)
        best[2].save(out / "panel_landscape.png")
        print(f"frame {i}: landscape view = {best[1]} "
              f"(OCR hh:mm={best[0][0]}, dates={best[0][1]}) -> "
              f"panel_raw_portrait.png / panel_landscape.png",
              file=sys.stderr)
        if args.describe:
            print(f"frame {i}: vision readback ...", file=sys.stderr)
            try:
                txt = describe_image(out / "panel_landscape.png")
                (out / f"frame{i}_analysis.txt").write_text(txt)
                print(f"--- frame {i} vision readback ---\n{txt}")
            except Exception as e:   # pragma: no cover — network/model path
                print(f"frame {i}: vision readback failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
