---
description: RoboDog ESP-Claw multi-session STRESS gate (DESIGN.md R7 soak) — runs N consecutive voice sessions back-to-back on the bench board, each asking a different corpus question, and asserts every session still completes the whole chain (no turn collapses back to idle, no board reset/panic). This is the sustained-load regression guard the single-turn e2e cannot provide: the first turn after a boot can pass while later turns die on a resource an earlier turn fragmented or never released. Runs through benchd (build/flash/gate from ONE commit); never writes .gate-head or hardware-e2e.md.
argument-hint: (no arguments - detects the bench board + test audio, runs the soak, returns PASS/FAIL with per-session evidence)
---

# RoboDog ESP-Claw Multi-Session STRESS Gate (R7 soak)

**Workflow ID**: $WORKFLOW_ID

## What this gate proves

The voice loop survives **repeated** turns, not just the first one after a
boot. `robodog_claw_e2e.py` proves one turn; this gate runs N sessions and
asserts each one reaches the whole chain:

```
OPEN_SESSION   rd_listen -> listen started
ASR_MATCH      corpus played into the mic -> ASR text= matches
ASR_LANG       ASR language= equals the corpus language
REPLY_LANG     voice reply language= shows the turn's directive
REPLY          agent reply: from the native agent seam
TTS            every chunk TTS-bracketed
CHUNKED_QUEUE  the WHOLE reply was queued and voiced (no dropped tail)
FULL_READOUT   the reply's tail words come back off the test mic
NO_RESET       no board reset/panic anywhere in the run
NO_COLLAPSE    no session ended in the silent/capture-fault path
               (`no usable utterance`, `capture fault`, `no SPIRAM`)
```

`NO_COLLAPSE` is the leg this gate exists for: the historical failure was
"hit listen -> straight back to idle" on every turn **after** the first, caused
by a resource the earlier turn leaked. A single-turn gate cannot see it.

DESIGN.md mandates **>= 5** sessions (`--runs 5` is the default and the floor).

## ⚠️ HARDWARE SAFETY — READ FIRST

1. This gate **drives audio** (test speaker into the board mic, test mic onto
   the board speaker) and opens voice turns with **`rd_listen`** over the
   USB-JTAG console. It is **exclusive**: it holds the board for the whole
   soak, so it must not run concurrently with `robodog-claw-e2e`,
   `robodog-claw-voice-verify`, or any flash.
2. **NEVER run esptool on the Cardputer** (`0x303A:0x8120`) or flash the RNode
   (`/dev/ttyUSB*`). The bench owns the RoboDog board (`0x303A:0x1001`); this
   gate never flashes — it runs on whatever build you name.
3. **Never write `$ARTIFACTS_DIR/.gate-head`** — only
   `robodog-production-health` owns that marker (#100 guard).
4. The soak is slow by design: ~60-90 s per session, so `--runs 5` is roughly
   5-8 minutes of board time plus the build/flash.

## Phase 0: BENCH (the only supported path)

The board lives on the bench host (op4) behind `benchd`. Run the soak there.

```bash
# 1. Build the commit under test (bench fetches it from origin).
B=$(bench submit build --head "$HEAD_SHA" --ref "$PR_REF" --family claw --class ci \
  --payload '{"env":{"CLAW_RT_MODEL":"Qwen/Qwen3-Omni-30B-A3B-Instruct",
                     "CLAW_RT_DISABLE_THINKING":"n"}}')
bench wait "$B"

# 2. Flash that exact build.
F=$(bench submit flash_capture --head "$HEAD_SHA" --build "$B" --family claw --class ci)
bench wait "$F"

# 3. Run the soak FROM that build's worktree.
S=$(bench submit stress --head "$HEAD_SHA" --build "$B" --family claw --class ci \
  --payload '{"args":["--runs","5"],
              "env":{"RD_ALSA_DEVICE":"plughw:CARD=Device",
                     "RD_SOUND_CARD":"Device","RD_PLAY_VOL":"0.9"}}')
bench wait "$S" --on-artifacts "$ARTIFACTS_DIR/claw-stress"
bench result "$S"
```

Or fold the soak into a single request — build + flash + gate + soak, one
lease, one verdict:

```bash
bench submit e2e --head "$HEAD_SHA" --ref "$PR_REF" --family claw --class ci \
  --payload '{"env":{"CLAW_RT_MODEL":"Qwen/Qwen3-Omni-30B-A3B-Instruct",
                     "CLAW_RT_DISABLE_THINKING":"n",
                     "RD_ALSA_DEVICE":"plughw:CARD=Device",
                     "RD_SOUND_CARD":"Device","RD_PLAY_VOL":"0.9",
                     "INCLUDE_STRESS":"1"},
              "args":["--corpus","capabilities"]}'
```

`INCLUDE_STRESS=1` runs the single-turn gate first and then the soak, so one
job proves both the turn and the sustained load.

`--build "$B"` keeps the gate honest: benchd runs
`robodog_claw_stress.py` from **that build's worktree**, never the persistent
clone. Without `--build` benchd logs a drift warning.

Diagnostics while it runs:

```bash
bench tail "$S"                              # follow the soak live
bench logs "$S" --artifact console_asr.log --grep "PANIC|collapse|no usable"
bench artifacts "$S" --out "$ARTIFACTS_DIR/claw-stress"
```

## Phase 1: READ THE VERDICT

`bench result "$S"` prints one assertion table per session plus the overall
verdict. A PASS requires **every** session to be green:

- any `NO_COLLAPSE: FAIL` is a firmware regression (a leaked resource), not a
  rig problem — the corpus is the same one the single-turn gate just passed;
- `RING`/`FULL_READOUT` may be `PASS-WEAK` when speaker→mic coupling is
  marginal (audible but no verbatim overlap); a real FAIL only when the test
  mic captured nothing;
- `ASR_MATCH` empty with `ever_speech=1` is an endpoint/ASR problem, not a
  capture problem — check `RT ASR http=` in the same console.

## Phase 2: ARTIFACT

Write `$ARTIFACTS_DIR/robodog-claw-stress/robodog-claw-stress.md` (the script
already writes it under the run dir; copy the summary up for the gate chain):

```markdown
# RoboDog ESP-Claw Multi-Session STRESS Results
**Generated**: {YYYY-MM-DD HH:MM}   **Workflow ID**: $WORKFLOW_ID
**Status**: ✅ PASS | ❌ FAIL
**Board**: Waveshare ESP32-S3 RLCD-4.2 (0x303A:0x1001) via bench (op4)
**Head**: {sha}   **Build job**: {id}   **Runs**: {N}
- session 1: OPEN_SESSION=… ASR_MATCH=… REPLY=… TTS=… CHUNKED_QUEUE=… NO_COLLAPSE=…
- session 2: …
## Per-session verbatim transcripts (the evidence)
```

Never writes `.gate-head`.

## Relationship to the other claw gates

| Gate | Proves |
|---|---|
| `robodog-claw-e2e` | ONE turn end-to-end (capture → ASR → reply → TTS → panel) |
| `robodog-claw-voice-verify` | the CHUNKED reply is read out to its END |
| **`robodog-claw-stress`** | **N turns in a row: nothing leaks, nothing collapses** |

All three run on the bench against the same build. Run them in that order:
turn → completeness → soak. The soak is last because it is the slowest and it
only makes sense once a single turn passes.
