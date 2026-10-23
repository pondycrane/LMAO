---
description: Commit changes, create PR with template + mandatory hermes hardware E2E evidence (hermes-hardware-e2e.md + robodog-voice-loop-e2e.md), mark ready for review. RoboDog hermes-adapted.
argument-hint: (no arguments - reads from workflow artifacts)
---

# RoboDog Hermes Finalize Pull Request

**Workflow ID**: $WORKFLOW_ID

## ⚠️ RoboDog Hermes Project Rules (MANDATORY — from AGENTS.md + README "Voice mode")

- **NEVER run esptool on the Cardputer** (`0x303A:0x8120`) or flash the RNode
  (`/dev/ttyUSB*`).
- The PR body MUST include the hermes hardware evidence from
  `$ARTIFACTS_DIR/hermes-hardware-e2e.md` AND `$ARTIFACTS_DIR/robodog-voice-loop-e2e.md`
  (results tables — or the loud SKIPPED warnings if no devices were attached). A
  PR without them is incomplete per the hermes feature contract (issue #267).
- Use the calendar's `hardware-e2e.md` if and only if it ALSO exists for this
  run (it is the calendar gate's artifact; do not substitute it for the hermes
  evidence).

## Your Mission

Finalize the implementation and create/update the PR:
1. Commit all changes
2. Push to remote
3. Create PR using project's template (if exists)
4. Mark PR as ready for review

## Phase 0: HARDWARE E2E GATE (MANDATORY — do this before anything else)

```bash
for ART in "$ARTIFACTS_DIR/hermes-hardware-e2e.md" "$ARTIFACTS_DIR/robodog-voice-loop-e2e.md"; do
  if [ ! -f "$ART" ]; then
    echo "FATAL: $ART does not exist — the mandatory hermes hardware E2E gates"
    echo "did not produce results. PR creation is blocked. Re-run:"
    echo "  archon workflow run robodog-hermes-dev (or run robodog-hermes-e2e then"
    echo "  robodog-voice-e2e against the branch)"
    exit 1
  fi
done
```

This check MUST run before any git add/commit/push.

## Phase 1: LOAD - Gather Context

### 1.1 Load Workflow Artifacts

```bash
cat $ARTIFACTS_DIR/plan-context.md
cat $ARTIFACTS_DIR/implementation.md
cat $ARTIFACTS_DIR/validation.md
cat $ARTIFACTS_DIR/hermes-hardware-e2e.md
cat $ARTIFACTS_DIR/robodog-voice-loop-e2e.md
```

Extract: plan title/summary, branch name, files changed, tests written,
validation results, hermes hardware E2E results (or SKIPPED warnings),
deviations.

### 1.2 Check for PR Template

Read `.github/pull_request_template.md`, `.github/PULL_REQUEST_TEMPLATE.md`, or
`docs/PULL_REQUEST_TEMPLATE.md` if present; fill every section.

### 1.3 Check for Existing PR

```bash
gh pr list --head $(git branch --show-current) --json number,url,state
```

- Existing → update. None → create.

**PHASE_1_CHECKPOINT:** [ ] artifacts loaded [ ] template/format known [ ] PR status known

## Phase 2: COMMIT - Stage and Commit Changes

Stage **only** the implementation files you edited — never `git add -A` /
`git add .` / `git add -u`. List them by name and verify with
`git status --porcelain` and `git diff --cached --name-only`.

Never stage scratch/review/PR-body artifacts or anything under `$ARTIFACTS_DIR`.

Commit with a descriptive message (from the plan; `Implements #…` when
relevant), then push: `git push origin HEAD`.

**PHASE_2_CHECKPOINT:** [ ] staged-only-files [ ] no sensitive files [ ] committed [ ] pushed

## Phase 3: CREATE/UPDATE - Pull Request

### 3.1 Prepare PR Body

Use the PR template if it exists; otherwise the default format:

```markdown
## Summary
{Brief description from plan summary}

## Changes
{file → action → what it does table}

## Tests
{from implementation.md}

## Validation
{from validation.md}
- [x] `bazel build //...` passes
- [x] ruff check / format passes (changed files)
- [x] mypy passes (changed files)
- [x] All unit tests pass
- [x] BUILD completeness — every changed .py belongs to a target
- [x] hermes firmware build (build_robodog_hermes) — PASS / UNVERIFIABLE

## Hardware E2E

{Insert the FULL contents of $ARTIFACTS_DIR/hermes-hardware-e2e.md AND
$ARTIFACTS_DIR/robodog-voice-loop-e2e.md here — results tables verbatim. If
either reports SKIPPED (board/test-audio absent), include that warning verbatim
and prominently; never omit the section.}

## Cursor evidence (state screen — issue #267)

Per-loop bootstrapped from `robodog-voice-e2e`'s artifacts: the CURSOR_LISTEN /
CURSOR_PROCESS / CURSOR_RESPOND assertion strings from the results table plus,
verbatim, `loopN/frames.json`, `loopN/frameN_analysis.txt` (LISTENING block /
PROCESSING `...` / RESPONDING `_` vision readbacks) and
`loopN/cursor_region_diff.txt` (RESPONDING steadiness). The fresh firmware
patch is `robodog_hermes_statescreen.patch` (≠ the rejected `robodog_hermes_ui.patch`;
#276 — underline+block / rotating-slash / flashing-underline / LVGL-bypass — was
NOT reused; see Implementation Notes).

## Notes

- PR #276 (`feat/robodog-hermes-ui-readability`) was REJECTED/superseded by this
  PR (fresh 3-state presentation per PRD). When this PR merges, close #276:
  `gh pr close 276 --comment "REJECTED — screen presentation implemented fresh in #<new> per
  PRD (pulsing block one cell ahead / pulsing `...` / steady `_`); the #276 canvas
  (underline+block, rotating slash, flashing underline, LVGL-bypass) was not reused."`

## Implementation Notes
{deviations + issues resolved}

**Plan**: `{plan-source-path}`   **Workflow ID**: `$WORKFLOW_ID`
```

### 3.2 Create or Update the PR

```bash
cat > $ARTIFACTS_DIR/pr-body.md <<'EOF'
{prepared-body}
EOF
gh pr create --title "{plan-title}" --body-file $ARTIFACTS_DIR/pr-body.md --base $BASE_BRANCH \
  || gh pr edit {existing} --body-file $ARTIFACTS_DIR/pr-body.md
gh pr ready {number} 2>/dev/null || true
```

### 3.3 Capture PR Info

```bash
gh pr view --json number,url,headRefName,baseRefName
echo "$(gh pr view --json number -q '.number')" > $ARTIFACTS_DIR/.pr-number
echo "$(gh pr view --json url -q '.url')"       > $ARTIFACTS_DIR/.pr-url
```

## Phase 4: ARTIFACT - Write PR Ready Status

Write `$ARTIFACTS_DIR/pr-ready.md` (#, URL, branch, status Ready for Review,
commit, evidence embedded). Summarize and STOP.
