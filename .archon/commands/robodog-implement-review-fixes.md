---
description: Implement RoboDog CRITICAL and HIGH review fixes — Bazel + firmware build after each fix, panel-capture verification for display-affecting fixes, commit + push to the PR branch. RoboDog-adapted.
argument-hint: (none - reads from consolidated review artifact)
---

# RoboDog Implement Review Fixes

**Workflow ID**: $WORKFLOW_ID

---

## ⚠️ RoboDog Project Rules (MANDATORY — from AGENTS.md + robodog_client/README.md)

**Hardware safety — violating these can brick physical devices:**

1. **RoboDog board** (Waveshare ESP32-S3 RLCD-4.2, VID `0x303A` PID `0x1001`)
   is flashed ONLY via `bazel run //robodog_client:flash_firmware -- --port <port>`
   (`robodog_client/flash.sh`, host esptool).
2. **NEVER run esptool on the Cardputer** (`0x303A:0x8120`) or flash the RNode
   via esptool — those devices are untouched by this step.
3. After a robodog flash the new firmware owns the panel — display-affecting
   fixes are "done" only when the panel shows them (screen capture + vision
   readback), never when the code merely compiles.

**Build rules:**

- **Bazel is the canonical build.** Every new `*.py` MUST belong to a BUILD
  target — a test without a `py_test` silently never runs (issue #87).
- Lint/type on changed files only: `ruff check` / `ruff format --check` / `mypy`.
- Vendored code is lint-exempt: `robodog_client/components/u8g2/**`,
  `u8g2_st7305/**`, `cardputer_client/lib/urns/**`, `rnode_firmware/esptool.py`.
- Do NOT remove Bazel runfiles workarounds (`sys.path` bootstraps,
  `imports = ["."]`, "Bazel sandbox" comments) — tests fail at module load
  without them (issue #87). Do NOT copy upstream sources into `components/`.

---

## IMPORTANT: Output Behavior

**Your output will be posted as a GitHub comment.** Keep working output minimal —
do not narrate each step; only output the final structured report at the end.

---

## Your Mission

Read the consolidated review artifact and implement all CRITICAL and HIGH
priority fixes. Add tests for fixed code if missing. Commit and push changes.
Report what was fixed, what wasn't (and why), and suggest follow-up issues.

**Output artifact**: `$ARTIFACTS_DIR/review/fix-report.md`
**Git action**: Commit AND push fixes to the PR branch
**GitHub action**: Post fix report comment

---

## Phase 1: LOAD - Get Fix List

### 1.1 Get PR Number from Registry

```bash
PR_NUMBER=$(cat $ARTIFACTS_DIR/.pr-number)
HEAD_BRANCH=$(gh pr view $PR_NUMBER --json headRefName --jq '.headRefName')
echo "PR: $PR_NUMBER, Branch: $HEAD_BRANCH"
```

### 1.2 Checkout the PR Branch

**CRITICAL: Work on the PR's actual branch, not a new branch.**

```bash
git fetch origin $HEAD_BRANCH
git checkout $HEAD_BRANCH
git pull origin $HEAD_BRANCH
```

### 1.3 Read Consolidated Review

```bash
cat $ARTIFACTS_DIR/review/consolidated-review.md
```

Extract CRITICAL + HIGH issues (with fixes), note MEDIUM/LOW for reporting.
Read the individual findings for full fix code when the consolidated lacks it
(`$ARTIFACTS_DIR/review/code-review-findings.md`, `error-handling-findings.md`,
`test-coverage-findings.md`, `docs-impact-findings.md`).

### 1.4 Check Current Git State

```bash
git status --porcelain
git branch --show-current
```

Verify you are on `$HEAD_BRANCH`.

**PHASE_1_CHECKPOINT:**
- [ ] PR number identified
- [ ] On the correct PR branch
- [ ] Consolidated review loaded
- [ ] CRITICAL/HIGH issues extracted

---

## Phase 2: IMPLEMENT - Apply Fixes

### 2.1 For Each CRITICAL / HIGH Issue

1. Read the file; apply the recommended fix.
2. Verify the fix builds:
   - Host: `bazel build //...` + `ruff check <changed files>`
   - Firmware (`robodog_client/main/**` or `components/**` touched):
     `bazel run //robodog_client:build_firmware`
3. Run the host wire selftest if `lma_proto.*` changed:
   `cc -std=c11 -Wall -Wextra robodog_client/main/lma_proto_selftest.c robodog_client/main/lma_proto.c -o /tmp/lma_selftest && /tmp/lma_selftest`
4. Track what changed.

### 2.2 Display-Affecting Fix

If the fix changes what the panel draws/shows (layout, status hints, row
logic, pictures, fonts), verify it from the pixels:

```bash
bazel run //robodog_client:build_firmware
bazel run //robodog_client:flash_firmware -- --port "${ROBODOG_PORT:-/dev/ttyACM0}"
python3 robodog_client/capture.py --port "${ROBODOG_PORT:-/dev/ttyACM0}" \
  --reset --n 3 --timeout 120 --describe \
  --out "$ARTIFACTS_DIR/robodog-shots-fixes"
```

Read `frameN_analysis.txt` (Qwen2.5-VL transcription) + the PNG. The fix is done
when the transcription shows the intended state. (The final `robodog-hardware-e2e`
gate re-captures anyway — this catches regressions while iterating.)

### 2.3 Test Coverage Gaps

1. Create/update the test file
2. Add tests for the fix
3. Wire new test files into `tests/BUILD` (issue #87)
4. `bazel test //tests:<target> --test_output=errors`

### 2.4 Handle Unfixable Issues

Document the reason (conflict / complex / unclear / risk) clearly.

**PHASE_2_CHECKPOINT:**
- [ ] All CRITICAL fixes attempted
- [ ] All HIGH fixes attempted
- [ ] Display-affecting fixes verified from the pixels
- [ ] Tests added; unfixable issues documented

---

## Phase 3: VALIDATE - Verify Fixes

```bash
bazel build //...
ruff check <changed-files> && ruff format --check <changed-files>
mypy <changed-files>
bazel test //tests:all --test_tag_filters=-requires_hardware --test_output=errors
```

If `robodog_client/main/**` changed: `bazel run //robodog_client:build_firmware`.
All must pass; fix anything introduced.

**PHASE_3_CHECKPOINT:**
- [ ] Bazel build passes
- [ ] ruff / mypy pass on changed files
- [ ] All unit tests pass
- [ ] Firmware compiles when main/ changed
- [ ] New `.py` files belong to a BUILD target

---

## Phase 4: COMMIT AND PUSH - Save and Push Changes

### 4.1 Stage Changes

Stage **only** the files you edited — never `git add -A` / `git add .` /
`git add -u`. Never stage `.pr-body.md`, `review/`, `*-report.md`, or anything
under `$ARTIFACTS_DIR`.

### 4.2 Commit

```bash
git commit -m "fix: Address RoboDog review findings (CRITICAL/HIGH)

Fixes applied:
- {brief list of fixes}

Tests added:
- {brief list}

Review artifacts: $ARTIFACTS_DIR/review/"
```

### 4.3 Push to PR Branch

```bash
git push origin $HEAD_BRANCH
# if divergent:
git pull --rebase origin $HEAD_BRANCH && git push origin $HEAD_BRANCH
```

**PHASE_4_CHECKPOINT:**
- [ ] Changes committed
- [ ] Changes pushed to PR branch

---

## Phase 5: GENERATE - Create Fix Report

Write to `$ARTIFACTS_DIR/review/fix-report.md`:

```markdown
# RoboDog Fix Report: PR #{number}

**Date**: {ISO timestamp}
**Status**: {COMPLETE | PARTIAL}
**Branch**: {HEAD_BRANCH}

## Summary
{2-3 sentence overview}

## Fixes Applied
### CRITICAL ({n}/{total}) — table: Issue | Location | Status | Details
### HIGH ({n}/{total}) — table: Issue | Location | Status | Details

## Panel-Capture Verification (display-affecting fixes)
| Frame | Vision readback (verbatim) | Verdict |
|-------|---------------------------|---------|
| frameN | `2026-10-15 21:25 / …` | ✅ intended state on panel |

## Tests Added
| Test File | Test Cases | For Issue |

## Not Fixed (Requires Manual Action)
| Issue | Severity | Location | Reason | Suggested action |

## MEDIUM Issues (User Decision Required)
…
```

Post the report as a comment on the PR
(`gh pr comment $PR_NUMBER --body "$(cat $ARTIFACTS_DIR/review/fix-report.md)"`).

---

## Success Criteria

- **CRITICAL/HIGH_DONE**: every CRITICAL and HIGH issue either fixed or documented
  as unfixable with a reason
- **PANEL_EVIDENCE**: display-affecting fixes show their intended state in a
  captured-frame readback (recorded in the report)
- **GREEN**: bazel build, ruff/format, mypy, unit tests, firmware build pass
- **PUSHED**: fixes committed + pushed to the PR branch
- **REPORT**: `$ARTIFACTS_DIR/review/fix-report.md` written + posted; hazard
  rules upheld (esptool only ever on the robodog board port)
