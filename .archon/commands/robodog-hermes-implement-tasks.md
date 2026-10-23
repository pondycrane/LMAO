---
description: Execute RoboDog hermes voice plan tasks with build checking after each change — Bazel host gate + hermes firmware build (build_robodog_hermes), voice-loop E2E verification for voice-path work, robodog hardware safety rules.
argument-hint: (no arguments - reads from workflow artifacts)
---

# RoboDog Hermes Implement Tasks

**Workflow ID**: $WORKFLOW_ID

## ⚠️ RoboDog Hermes Project Rules (MANDATORY — from AGENTS.md + README "Voice mode")

**Hardware safety — violating these can brick physical devices:**

1. **NEVER run esptool on the Cardputer** (`0x303A:0x8120`) — no flashing, no
   `chip_id`, no probing.
2. **NEVER flash the RNode** (`/dev/ttyUSB*`) — web flasher only.
3. The hermes board (`0x303A:0x1001`) is flashed ONLY via
   `bazel run //robodog_client:flash_robodog_hermes`. This replaces the
   calendar app — do NOT let any step run `//robodog_client:flash_firmware`
   (calendar) or the calendar gate during this workflow.
4. **Never write `$ARTIFACTS_DIR/.gate-head`** — only `robodog-production-health`
   owns that marker (issue #100).
5. Source is fetched, never vendored: hermes firmware builds via
   `voice/fetch_xiaozhi.sh` (pinned upstream + sha256) into the gitignored
   `.xiaozhi/`; the bridge/ASR/TTS sources are LMAO-owned. Never `cp -r` an
   upstream tree into committed dirs.

## Your Mission

Execute every task in `$ARTIFACTS_DIR/plan.md`. Implement the code, check
after each change, and capture the evidence each task's verification calls for.

## Phase 1: LOAD CONTEXT

```bash
cat $ARTIFACTS_DIR/plan-context.md
cat $ARTIFACTS_DIR/plan.md
cat $ARTIFACTS_DIR/implementation.md
git diff --stat # base → working tree
```

## Phase 2: WORK IN THE RUN TREE

Your shell default is the ORIGINAL checkout, possibly on another branch. Run in
the run's branch tree:

```bash
git worktree list --porcelain | grep -E "^(worktree|branch)"
# prefer the worktree for this run's branch; else current checkout
```

## Phase 3: IMPLEMENT TASK BY TASK (verify each)

For each task:

1. Implement in small, reviewable steps; every new `*.py` gets a Bazel target
   (issue #87) — the voice tools are `sh_binary` at
   `//robodog_client:hermes_voice_e2e`/`:make_corpus` unless #271 landed.
2. After each change, run the host check:
   ```bash
   bazel build //robodog_client:* -- //robodog_client:hermes_voice_e2e 2>/dev/null || bazel build //...
   ruff check <changed-py>; mypy <changed-py>   # changed files only
   ```
   Do NOT run the hardware gates here — they come next in the workflow. Fix
   errors before moving on; never leave a failing build between tasks.
3. When the task touches `robodog_client/main/**`/`components/**` or
   `voice/**` firmware sources, run the firmware compile check:
   ```bash
   bazel run //robodog_client:build_robodog_hermes
   ```
4. When the task touches the voice-path python (bridge / orchestrator / corpus),
   exercise it on the live rig when the board is attached:
   ```bash
   export XDG_RUNTIME_DIR=/run/user/$(id -u)
   E2E_RUNS=1 ~/xiaozhi/.venv/bin/python robodog_client/voice/hermes_voice_e2e.py --corpus time --artifacts /tmp/e2e-task
   ```
   Read the asserts (OPEN_SESSION / ASR_MATCH / BRAIN_REPLY / RING / SUBTITLE /
   READOUT_COMPLETE / POST_READOUT_RELISTEN / CURSOR_LISTEN / CURSOR_PROCESS /
   CURSOR_RESPOND) and record them in `$ARTIFACTS_DIR/implementation.md`. If the
   mic ASR is flaky, record the documented `E2E_STT` fallback path — do not
   re-run the whole battery hoping for green; the hard gates come next.
5. Append the task result + evidence to `$ARTIFACTS_DIR/implementation.md`.

## Phase 4: WRAP UP

- Re-run the host build once more; ensure no stray files staged.
- Write a final summary in `$ARTIFACTS_DIR/implementation.md` (tasks done,
  files changed, tests, evidence captured, deviations from plan).
- Do NOT commit/push — that is `robodog-hermes-finalize-pr`'s job, after the
  full gate chain.
