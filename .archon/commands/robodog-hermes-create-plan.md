---
description: Create comprehensive implementation plan for RoboDog hermes voice work (XiaoZhi firmware + bridge/E2E) — every voice/display/data change must be validated by the acoustic voice-loop E2E (robodog-voice-e2e: RD_LISTEN session open, ASR transcript match, Hermes reply, test-mic ring closure, RLCD subtitle readback) and/or the hermes screen capture (robodog-hermes-e2e), never by re-derivation. Bazel-native, robodog hardware safety rules baked in.
argument-hint: <feature description | path/to/prd.md>
---

# RoboDog Hermes Create Implementation Plan

**Input**: $ARGUMENTS
**Workflow ID**: $WORKFLOW_ID

## ⚠️ RoboDog Hermes Project Rules (MANDATORY — from AGENTS.md + README "Voice mode")

**Hardware safety — violating these can brick physical devices:**

1. **NEVER run esptool on the Cardputer** (`0x303A:0x8120`) — no flashing, no
   `chip_id`, no probing.
2. **NEVER flash the RNode** (`/dev/ttyUSB*`) — web flasher only.
3. The hermes board (Waveshare ESP32-S3 RLCD-4.2, `0x303A:0x1001`) is flashed
   ONLY via `bazel run //robodog_client:flash_robodog_hermes` (host esptool;
   `robodog_client/xiaozhi_flash.sh`). This REPLACES the calendar app. Never
   run the calendar gate (`robodog-hardware-e2e`) during hermes work — it
   re-flashes the calendar firmware and destroys the hermes image under test.
4. `capture.py --reset` is the only other esptool call (same board, host reset).
5. **Never write `$ARTIFACTS_DIR/.gate-head`** — only `robodog-production-health`
   owns that marker (issue #100 guard).

**Hermes verification contract (issue #267):**
- Session open is the **RD_LISTEN software button** (serial trigger over the
  same USB-JTAG console as the capture hook) — the WN9 wake word is unreliable
  and is NOT used.
- Acoustic legs: test speaker drives `robodog_client/voice/corpus/` pinned
  command WAVs into the board mic (volume ≤ `RD_PLAY_VOL=0.7`, settle
  ≥ `RD_PLAY_SETTLE=4.5s` after `StartListening()` or the command sits below the
  VAD threshold); test mic captures the board speaker's reply for ring closure.
- Fallback: real-speech ASR is flaky on freshly-hammered sessions — the plan
  must accept the documented `E2E_STT` injection fallback
  (`POST /xiaozhi/inject`) for those loops and report `FAIL (mic ASR) ->
  E2E_STT inject fallback` verbatim, never silently pass.
- Runtime: the bridge/E2E run under the ops venv (sh_binary bazel targets
  `//robodog_client:hermes_voice_e2e`, `:make_corpus`); converting them to
  py_binary depends on issue #271 (lmao_pip toolchain) — do NOT assume it.

## Your Mission

Create a comprehensive, executable implementation plan for the feature in
$ARGUMENTS. RoboDog-adapted: every accept criterion must be a check the gates
below can prove.

## Phase 1: UNDERSTAND

Research before planning — the single-source README, `robodog_client/voice/*`
(bridge, `hermes_voice_e2e.py`, corpus, build/flash scripts, the RD_SHOT +
RD_LISTEN patches), and any referenced issues/PRs. Check the open PR graph via
the lmao knowledge graph tooling if the feature overlaps existing work.

## Phase 2: SCOPING

Restate the goal. Identify the tasks. For each task classify the verification
path:
- **Voice-loop-affected** → must be proven by `robodog-voice-e2e`
  (`bazel run //robodog_client:hermes_voice_e2e`: OPEN_SESSION / ASR_MATCH /
  BRAIN_REPLY / RING / SUBTITLE asserts).
- **Panel/UI-affected** → must be proven by `robodog-hermes-e2e` (screen
  capture + vision readback of the ST7305 pixels).
- **Host/bridge logic** → proven by `robodog-hermes-validate` unit/lint/build.
List the concrete evidence each task will produce (journal lines, transcripts,
frame PNGs, re-ASR text).

## Phase 3: WRITE THE PLAN

Write `$ARTIFACTS_DIR/plan.md` (the standard lmao plan format). Include:
- Branch name (no `/`), base branch, task breakdown with verification steps.
- BUILD wiring tasks: every new `*.py` must belong to a Bazel target (issue
  #87) OR be in the documented exempt list; the `@lmao_pip` set does NOT yet
  carry the voice stack (faster-whisper/fastapi/numpy…) — keep it in the ops
  venv unless #271 lands.
- Which of `robodog-hermes-e2e` / `robodog-voice-e2e` gate the feature.
- Risks: board battery state, ASR flakiness, ring/subtitle transience.

## Output

`$ARTIFACTS_DIR/plan.md` written and complete. Final summary: the goal, the
task list, the verification gates per task, risks.
