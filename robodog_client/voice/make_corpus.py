#!/usr/bin/env python3
"""Regenerate the pinned RoboDog voice-loop E2E utterance corpus.

The corpus is the deterministic acoustic drive for the voice-loop E2E
(issue #267): short commands with known transcripts, synthesized with the SAME
native timbre the endpoint picks for that language (TTS_BASE_URL). Deterministic
speech makes ASR matching reliable.

Two languages are pinned so the dual-language path is gated, not just claimed:

- English commands (voice `aiden`) exercise the default English reply path.
- A Chinese command (voice `serena`, `language: chinese`) exercises the
  auto-detected ASR language (`ASR language=zh`), the Traditional-Chinese reply
  directive, CJK rendering on the RLCD and the Chinese-native TTS voice.  Its
  transcript uses only characters that are IDENTICAL in Simplified and
  Traditional (你好世界) because faster-whisper may emit either script; a
  script-dependent phrase would make the ASR match flap.

The committed WAV fixtures are small (~50-200 KB each) and the manifest pins
their sha256; re-run this script to regen/verify them (needs the TTS endpoint):

    python3 make_corpus.py                 # regen corpus/ from the TTS endpoint
    python3 make_corpus.py --only zh_hello # regen ONE fixture (manifest merged)
    python3 make_corpus.py --verify        # check committed fixtures match the sha map
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

# English transcripts stay lowercase + ASCII so normalize(transcript) == the
# string faster-whisper produces for the TTS-spoken command.  A record may pin
# `voice` / `language`; the defaults are the English-native timbre and English.
COMMANDS = [
    {"transcript": "what time is it", "slug": "time"},
    {"transcript": "tell me a joke", "slug": "joke"},
    {"transcript": "how are you today", "slug": "greeting"},
    {"transcript": "what can you do", "slug": "capabilities"},
    {"transcript": "count slowly from twenty to thirty", "slug": "count"},
    # Chinese (Mandarin) leg: script-invariant characters so the ASR text
    # matches regardless of the Simplified/Traditional output script.
    # Comma-free and 0.8x on purpose: the TTS inserts a >800 ms pause at an
    # internal comma, which the board's 800 ms trailing-silence VAD takes for
    # end-of-utterance and truncates the turn at the clause (measured: a
    # "你好世界，今天很好" drive reached Whisper as just "你好世界").  The
    # pinned sample is 2.5 s at 0.7x, which decodes fully through the test
    # speaker → board mic; a longer/faster sample of the same text decoded as
    # "事情今天很好" on the same rig (the model's output length varies per
    # call, so the fixture is pinned by sha rather than regenerated blindly).
    # play_vol: the fixture's own drive level — the rig's speaker→board-mic
    # coupling loses Chinese at the English default (0.7 measured "你好" only;
    # 0.9 decoded the whole sentence), so the corpus pins it.
    {"transcript": "你好世界今天很好", "slug": "zh_hello",
     "language": "chinese", "voice": "serena", "speed": 0.7,
     "play_vol": 0.9},
]

TTS_BASE_URL = os.environ.get("TTS_BASE_URL", "http://spark-5307:8004").rstrip("/")
TTS_MODEL = os.environ.get("TTS_MODEL", "qwen3-tts")
# Pinned, not empty: a fixture must be reproducible even if the server's
# language inference ever changes. `aiden` is the English-native timbre the
# server picks for an empty voice on English text.
TTS_VOICE = os.environ.get("TTS_VOICE", "aiden")
TTS_API_KEY = os.environ.get("TTS_API_KEY", "dummy-local-noauth")

HERE = Path(__file__).resolve().parent
CORPUS = HERE / "corpus"
MANIFEST = CORPUS / "corpus.json"


def synthesize(text: str, voice: str = TTS_VOICE,
               language: str = "english", speed: float = 1.0) -> bytes:
    base = TTS_BASE_URL
    if not base.endswith("/v1"):
        base += "/v1"
    body = json.dumps({
        "model": TTS_MODEL,
        "input": text,
        "voice": voice,
        "language": language,
        "response_format": "wav",
        "speed": speed,
    }).encode()
    req = urllib.request.Request(
        base + "/audio/speech", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {TTS_API_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        return r.read()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true",
                    help="check committed fixtures against the pinned sha256 map")
    ap.add_argument("--only", metavar="SLUG",
                    help="regenerate ONE fixture and merge it into the manifest "
                         "(keeps every other pinned fixture untouched)")
    args = ap.parse_args()
    CORPUS.mkdir(parents=True, exist_ok=True)

    if args.verify:
        manifest = json.loads(MANIFEST.read_text())
        bad = []
        for slug, rec in manifest.items():
            p = CORPUS / rec["wav"]
            if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != rec["sha256"]:
                bad.append(slug)
        if bad:
            sys.exit(f"corpus mismatch for: {', '.join(bad)} (rerun make_corpus.py)")
        print(f"corpus ok: {len(manifest)} pinned fixtures match their sha256")
        return

    # --only merges into the existing manifest so re-synthesizing one fixture
    # never invalidates the other pinned WAVs.
    manifest = json.loads(MANIFEST.read_text()) if args.only else {}
    if args.only and args.only not in {c["slug"] for c in COMMANDS}:
        sys.exit(f"unknown slug '{args.only}': "
                 f"{sorted(c['slug'] for c in COMMANDS)}")
    for cmd in COMMANDS:
        slug = cmd["slug"]
        if args.only and slug != args.only:
            continue
        transcript = cmd["transcript"]
        language = cmd.get("language", "english")
        voice = cmd.get("voice", TTS_VOICE)
        speed = cmd.get("speed", 1.0)
        wav = synthesize(transcript, voice=voice, language=language, speed=speed)
        (CORPUS / f"{slug}.wav").write_bytes(wav)
        (CORPUS / f"{slug}.txt").write_text(transcript + "\n")
        manifest[slug] = {
            "transcript": transcript,
            "wav": f"{slug}.wav",
            "sha256": hashlib.sha256(wav).hexdigest(),
            "voice": voice,
            "language": language,
            "speed": speed,
            "play_vol": cmd.get("play_vol", 0.7),
            "model": TTS_MODEL,
        }
        print(f"{slug}: {len(wav)} bytes <- {transcript!r} "
              f"({language}, {voice}, {speed}x)")
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {MANIFEST}")


if __name__ == "__main__":
    main()
