#!/usr/bin/env python3
"""RoboDog ESP-Claw multi-session STRESS gate (DESIGN.md R7 soak).

Runs the voice loop several times back-to-back, each session asking a
DIFFERENT question (corpus cycle), and asserts every session still completes
the whole chain.  This is the sustained-load regression guard: the first turn
after a boot can pass while later turns die on a resource that the previous
turn's buffers fragmented or never released — exactly the class of failure
that made the board drop straight back to idle ("hit listen -> idle") on every
turn after the first.

Per session (the same legs as `robodog_claw_e2e.py --voice-verify`, reused
directly so there is ONE copy of the loop):
    OPEN_SESSION   `rd_listen` -> `listen started`
    ASR_MATCH      corpus played into the mic -> `ASR text=` matches
    REPLY          `agent reply:` from the native agent seam
    TTS            every chunk TTS-bracketed
    CHUNKED_QUEUE  the WHOLE reply was queued and voiced (no dropped tail)
    FULL_READOUT   the reply's tail words come back off the test mic
    NO_RESET       no board reset/panic anywhere in the run

Plus the turn-health leg this gate exists for:
    NO_COLLAPSE    no session ended in the silent/capture-fault path
                   (`no usable utterance`, `capture fault`, `no SPIRAM`)
                   instead of reaching ASR.

Never writes `hardware-e2e.md` or `.gate-head` (only the production-health
gate owns that marker).

    python3 robodog_client/esp-claw/robodog_claw_stress.py [--runs N]
"""

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import robodog_claw_e2e as e2e  # noqa: E402  (one copy of the voice loop)

TAG = "robodog-claw-stress"
CYCLE = ["time", "joke", "greeting", "capabilities", "count"]
LEGS = (
    "OPEN_SESSION",
    "ASR_MATCH",
    "REPLY",
    "TTS",
    "CHUNKED_QUEUE",
    "FULL_READOUT",
    "NO_COLLAPSE",
    "NO_RESET",
)
# Markers proving a session never got as far as ASR because the board could
# not capture/buffer the audio (the "back to idle" failure mode).
COLLAPSE_RE = (
    "no usable utterance",
    "capture fault",
    "no SPIRAM",
    "utterance buffer growth failed",
    "capture stream produced no audio",
)
# An ASR miss (empty transcription, or a transcript below the overlap floor)
# is NOT a firmware collapse: the board captured and posted the audio.  It is
# reported by ASR_MATCH; NO_COLLAPSE must stay a firmware-path assertion.


def log(msg):
    print(f"[stress] {time.strftime('%H:%M:%S')} {msg}", flush=True)


def ok(value):
    return value is not None and (value.startswith("PASS") or value.startswith("SKIP"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5,
                    help="consecutive voice sessions (default 5; DESIGN.md "
                         "mandates >= 5)")
    ap.add_argument("--artifacts", default=str(Path.home() / "artifacts"))
    ap.add_argument("--corpus-cycle", default="auto",
                    help="comma-separated corpus slugs cycled across sessions "
                         "('auto' = time,joke,greeting,capabilities,count)")
    args = ap.parse_args()

    artifacts = Path(args.artifacts) / TAG
    artifacts.mkdir(parents=True, exist_ok=True)

    e2e.DETECTED_PORT = e2e.detect_board_port()
    card = e2e.detect_sound_card()
    log(f"board={e2e.DETECTED_PORT or 'NONE'} sound='{card}' runs={args.runs}")
    skips = []
    if not e2e.DETECTED_PORT:
        skips.append("ROBODOG BOARD NOT DETECTED")
    if not card:
        skips.append("TEST AUDIO CARD NOT DETECTED (ALSA USB)")
    if skips:
        print("\nLOUD SKIP: " + "; ".join(skips) + "\n")
        (artifacts / "SKIPPED.txt").write_text("; ".join(skips) + "\n")
        return 0

    man = json.loads((e2e.HERE.parent / "voice" / "corpus" / "corpus.json").read_text())
    slugs = (
        CYCLE
        if args.corpus_cycle.strip().lower() == "auto"
        else [s.strip() for s in args.corpus_cycle.split(",") if s.strip()]
    )
    unknown = [s for s in slugs if s not in man]
    if unknown:
        sys.exit(f"unknown corpus slugs {unknown}: {sorted(man)}")
    log(f"corpus cycle: {', '.join(slugs)}")

    results = []
    for i in range(args.runs):
        instr = man[slugs[i % len(slugs)]]
        log(f"session {i + 1}/{args.runs}: {instr['transcript']!r}")
        r = e2e.run_loop(i, instr, artifacts, voice_verify=True)
        r["corpus"] = instr["transcript"]
        # NO_COLLAPSE: the console scan e2e already ran for this session
        # recorded every panic/reset line; the capture-fault markers are
        # recovered from the same run's failure diagnostics.
        diag = " ".join(
            v for v in r["asserts"].values() if isinstance(v, str)
        ) + " " + r["artifacts"].get("capture_diag", "")
        # NO_COLLAPSE is the FIRMWARE-COLLAPSE leg: it FAILs only when the
        # board could not capture/buffer the audio (SPIRAM cliff, stalled
        # capture stream) or never started listening at all — the failure
        # class where a listen silently ends in `idle` on later turns.  An
        # EMPTY transcription is an acoustic/ASR miss (the board captured and
        # posted the utterance); ASR_MATCH already FAILs that, so it must not
        # be misreported here as a firmware collapse.
        no_asr_line = (
            r["asserts"].get("ASR_MATCH", "").startswith("FAIL")
            and "no `ASR text=`" in r["asserts"].get("ASR_MATCH", "")
        )
        collapsed = any(marker in diag for marker in COLLAPSE_RE) or no_asr_line
        if collapsed:
            r["asserts"]["NO_COLLAPSE"] = (
                "FAIL session ended before ASR (silent/capture-fault path) — "
                "the board dropped back to idle instead of listening"
            )
        elif "EMPTY transcription" in r["asserts"].get("ASR_MATCH", ""):
            r["asserts"]["NO_COLLAPSE"] = (
                "PASS board captured + posted audio (no capture fault); the "
                "session's miss was acoustic (ASR heard no words)"
            )
        else:
            r["asserts"]["NO_COLLAPSE"] = (
                "PASS reached ASR (no capture fault / OOM collapse)"
            )
        results.append(r)
        for k in LEGS:
            log(f"{k}: {r['asserts'].get(k, 'FAIL (missing)')}")
        if i < args.runs - 1:
            time.sleep(3)

    def passed(r):
        return all(ok(r["asserts"].get(k, "FAIL")) for k in LEGS)

    overall = all(passed(r) for r in results)
    lines = [
        "# RoboDog ESP-Claw Multi-Session Stress\n",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M')}",
        f"Board: {e2e.DETECTED_PORT}",
        f"Sessions: {args.runs} (corpus cycle {', '.join(slugs)})\n",
    ]
    for r in results:
        lines.append(f"## session {r['loop']}")
        lines.append(f"- Corpus: {r.get('corpus', '')!r}")
        for k in LEGS:
            lines.append(f"- **{k}**: {r['asserts'].get(k, 'FAIL (missing)')}")
        lines.append(f"- ASR verbatim: {r['artifacts'].get('asr', '')!r}")
        lines.append("")
    lines.append(f"Overall: {'PASS' if overall else 'FAIL'}")
    (artifacts / f"{TAG}.md").write_text("\n".join(lines))
    print("\n".join(lines))
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
