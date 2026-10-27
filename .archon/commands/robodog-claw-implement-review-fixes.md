---
description: Implement RoboDog ESP-Claw CRITICAL and HIGH review fixes — Bazel + esp-claw firmware build (build_esp_claw.sh with the live ASR/TTS endpoint URLs) after each fix, flash_esp_claw.sh re-flash when firmware changed (the claw e2e gate does NOT reflash), commit + push to the PR branch. Claw-adapted.
argument-hint: (none - reads from consolidated review artifact)
---

# RoboDog ESP-Claw Implement Review Fixes

**Workflow ID**: $WORKFLOW_ID

## ⚠️ RoboDog ESP-Claw Project Rules (MANDATORY — from AGENTS.md + robodog_client/esp-claw/DESIGN.md)

1. **esptool is only for the RLCD board** (`0x303A:0x1001`), and only via
   `robodog_client/esp-claw/flash_esp_claw.sh`. **NEVER** run esptool on the
   Cardputer (`0x303A:0x8120`) or flash the RNode (`/dev/ttyUSB*`) — those stay
   esptool-banned under every workflow.
2. **No vendoring**: the esp-claw upstream is fetched sha256-pinned into the
   gitignored `robodog_client/.esp-claw/`; never copy upstream `.c/.h` into the
   repo. bmgr manifest paths are rewritten to `/workspace/...` absolute by
   `rewrite_component_paths.py`, `cfg_ver=1` NVS is mandatory (provisioned by
   `flash_esp_claw.sh` @0xA000).
3. **DRY**: put shared firmware protocol code in `firmware_common/` first —
   copy-pasting into `robodog_client/esp-claw/` is a DRY violation.
4. **The firmware arch/behavior rules live in `robodog_client/esp-claw/DESIGN.md`**
   — read the relevant Phase 0/1/2 sections before touching firmware; keep
   DESIGN.md's claims in sync with code changes (e.g. the TTS real-time pacing
   + PA/unmute/volume re-assert requirements).
5. **Never write `$ARTIFACTS_DIR/.gate-head`** — `robodog-production-health`
   owns it (issue #100).

## Your Mission

Implement the consolidated CRITICAL and HIGH review findings for the ESP-Claw
feature. This node runs BEFORE the final gate chain re-run, so the PR branch
ends up reviewed-clean and (if firmware changed) **re-flashed** — the
`robodog-claw-e2e` gate explicitly does NOT reflash, so this is the only place
the fixed image gets onto the board.

## Phase 1: LOAD THE FINDINGS

```bash
ls $ARTIFACTS_DIR/review/
cat $ARTIFACTS_DIR/review/*/*report.md   # or the consolidated review artifact
git status --porcelain && git log --oneline -3
```

## Phase 2: FIX (verify each)

Work in the run tree (`git worktree list --porcelain` → prefer the run branch's
worktree). For each CRITICAL/HIGH finding:

1. Apply the smallest correct fix. Do not reformat files outside the fix.
2. After each fix:
   ```bash
   bazel build //...
   ruff check <changed-py> && ruff format --check <changed-py>
   mypy <changed-py>
   bazel test //tests:all --test_tag_filters=-requires_hardware --test_output=errors
   ```
   When `robodog_client/esp-claw/**` firmware/overlay sources changed, ALSO
   compile the real firmware (this is the only build that proves the overlay +
   pinned upstream tree together; the plain `bazel run
   //robodog_client:build_robodog_claw` bakes dead spark-5307 endpoints):
   ```bash
   CLAW_IDF_IMG=espressif/idf:v5.5.4 \
   CLAW_ASR_URL=http://192.168.50.153:11801/v1/audio/transcriptions \
   CLAW_TTS_URL=http://192.168.50.153:11804/v1/audio/speech \
   bash robodog_client/esp-claw/build_esp_claw.sh
   ```
3. **Re-flash the board when the firmware image changed** (the fixed image must
   be what the final `robodog-claw-e2e` gate runs — it never reflashes):
   ```bash
   CLAW_IDF_IMG=espressif/idf:v5.5.4 bash robodog_client/esp-claw/flash_esp_claw.sh
   # No board (no 0x303A:0x1001)? State it in review-fixes.md and stop — the
   # e2e gate will loud-skip. NEVER flash any other device.
   ```
4. For voice-path fixes, exercise the loop on the rig when the board is
   attached (record the asserts to the artifact):
   ```bash
   python3 robodog_client/esp-claw/robodog_claw_e2e.py --corpus capabilities \
     --artifacts "$ARTIFACTS_DIR"
   ```
   (`RING` may be `PASS-WEAK` for marginal speaker→mic coupling and still proves
   audible TTS; a fail is only when the test mic captured nothing.)
5. Record the disposition in `$ARTIFACTS_DIR/review-fixes.md`
   (finding → fix → verification → fixed/declined-with-reason).

## Phase 3: COMMIT + PUSH

Stage only the touched implementation files (never scratch/artifacts); commit
with a message naming the findings; push to the PR branch.

When this reviewer was started on an existing PR, checkout-pr isolated the
review on a per-run branch and registered it at `$ARTIFACTS_DIR/.review-branch`
(head ref at `$ARTIFACTS_DIR/.pr-head`) — the PR branch itself is checked out
in the DEVELOPER's main worktree, so never `--ignore-other-worktrees` it here.
Commit on the current branch and push its head to the PR head ref:
`git push --force-with-lease origin HEAD:$(cat $ARTIFACTS_DIR/.pr-head)` (fall
back to pushing the ordinary PR branch only when `.review-branch` is absent).

## Phase 4: WRAP UP

Summarize: findings fixed (CRITICAL n/HIGH m), build/flash verification
(including which firmware commit SHA is on the board), and the commit sha. The
workflow's final gate chain re-runs next.
