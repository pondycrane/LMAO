---
description: Implement RoboDog hermes CRITICAL and HIGH review fixes — Bazel + hermes firmware build after each fix, voice-loop E2E verification for voice-path fixes, commit + push to the PR branch. Hermes-adapted.
argument-hint: (none - reads from consolidated review artifact)
---

# RoboDog Hermes Implement Review Fixes

**Workflow ID**: $WORKFLOW_ID

## ⚠️ RoboDog Hermes Project Rules (MANDATORY — from AGENTS.md + README "Voice mode")

1. **NEVER run esptool on the Cardputer** (`0x303A:0x8120`) or the RNode
   (`/dev/ttyUSB*`). The hermes board (`0x303A:0x1001`) flashes ONLY via
   `bazel run //robodog_client:flash_robodog_hermes` (that happens in the hermes
   hardware gates, not here).
2. **Never write `$ARTIFACTS_DIR/.gate-head`** — `robodog-production-health`
   owns it (issue #100).
3. The firmware is fetched, never vendored; the ops venv (`~/xiaozhi/.venv`) is
   the voice runtime — converting to bazel py_binary is issue #271's scope, not
   this fix round.

## Your Mission

Implement the consolidated CRITICAL and HIGH review findings for the hermes
voice feature. This node runs BEFORE the final gate chain re-run, so the PR
branch ends up reviewed-clean.

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
   When firmware sources changed: `bazel run //robodog_client:build_robodog_hermes`.
3. For voice-path fixes, exercise the loop on the rig when the board is
   attached (record the asserts to the artifact):
   ```bash
   export XDG_RUNTIME_DIR=/run/user/$(id -u)
   E2E_RUNS=1 ~/xiaozhi/.venv/bin/python robodog_client/voice/hermes_voice_e2e.py --corpus time
   ```
4. Record the disposition in `$ARTIFACTS_DIR/review-fixes.md`
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

Summarize: findings fixed (CRITICAL n/HIGH m), verification evidence, and the
commit sha. The workflow's final gate chain re-runs next.
