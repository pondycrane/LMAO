#!/usr/bin/env python3
"""Regenerate the pinned RoboDog voice-loop E2E utterance corpus.

The corpus is the deterministic acoustic drive for the hermes voice-loop E2E
(issue #267): a fixed set of short ENGLISH commands with known transcripts,
synthesized with the SAME Kokoro-82M voice the bridge speaks with
(TTS_BASE_URL/TTS_VOICE, see lmao-robodog-voice.service). Deterministic speech
makes ASR matching reliable, and English transcripts keep the RLCD subtitle
ASCII so capture.py --describe can read it back.

The committed WAV fixtures are small (~80 KB each) and the manifest pins their
sha256; re-run this script to regen/verify them (needs the TTS endpoint):

    python3 make_corpus.py            # regen corpus/ from the TTS endpoint
    python3 make_corpus.py --verify   # check committed fixtures match the sha map
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

# Keep transcripts lowercase + ASCII so normalize(transcript) == the string
# faster-whisper should produce for the TTS-spoken command.
COMMANDS = [
    ("what time is it", "time"),
    ("tell me a joke", "joke"),
    ("how are you today", "greeting"),
    ("what can you do", "capabilities"),
    ("count slowly from twenty to thirty", "count"),
]

TTS_BASE_URL = os.environ.get("TTS_BASE_URL", "http://spark-5307:8004").rstrip("/")
TTS_MODEL = os.environ.get("TTS_MODEL", "kokoro-82m")
TTS_VOICE = os.environ.get("TTS_VOICE", "af_heart")
TTS_API_KEY = os.environ.get("TTS_API_KEY", "dummy-local-noauth")

HERE = Path(__file__).resolve().parent
CORPUS = HERE / "corpus"
MANIFEST = CORPUS / "corpus.json"


def synthesize(text: str) -> bytes:
    base = TTS_BASE_URL
    if not base.endswith("/v1"):
        base += "/v1"
    body = json.dumps({
        "model": TTS_MODEL,
        "input": text,
        "voice": TTS_VOICE,
        "response_format": "wav",
        "speed": 1.0,
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

    manifest = {}
    for transcript, slug in COMMANDS:
        wav = synthesize(transcript)
        (CORPUS / f"{slug}.wav").write_bytes(wav)
        (CORPUS / f"{slug}.txt").write_text(transcript + "\n")
        manifest[slug] = {
            "transcript": transcript,
            "wav": f"{slug}.wav",
            "sha256": hashlib.sha256(wav).hexdigest(),
            "voice": TTS_VOICE,
            "model": TTS_MODEL,
        }
        print(f"{slug}: {len(wav)} bytes <- '{transcript}'")
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {MANIFEST}")


if __name__ == "__main__":
    main()
