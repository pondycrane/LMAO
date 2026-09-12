---
description: Commit smart-irrigation changes, create the PR with mandatory hardware E2E evidence, mark ready for review.
argument-hint: (no arguments - reads from workflow artifacts)
---

# Smart Irrigation — Finalize PR

**Workflow ID**: $WORKFLOW_ID

## ⚠️ Rules (MANDATORY)

- Never esptool; never touch devices in this step.
- The PR body MUST include the full hardware E2E evidence from
  `$ARTIFACTS_DIR/hardware-e2e.md` (results table or the loud
  SKIPPED/UNVERIFIABLE warning) — a PR without it is incomplete.

---

## Your Mission

Commit the implementation, push, and create (or update) the PR, ready for
review.

---

## Phase 0: HARDWARE E2E GATE (do this before anything else)

<!-- Mirror of the gate in the workflow; keep in sync -->

```bash
if [ ! -f "$ARTIFACTS_DIR/hardware-e2e.md" ]; then
  echo "FATAL: $ARTIFACTS_DIR/hardware-e2e.md is missing — the mandatory"
  echo "hardware E2E gate did not produce results. PR creation is blocked."
  echo "Re-run the gate chain (irrigation-validate → irrigation-hardware-e2e)."
  exit 1
fi
```

MUST run before any `git add`/`commit`/`push`. On failure: output the error and STOP.

---

## Phase 1: LOAD CONTEXT

```bash
cat $ARTIFACTS_DIR/plan-context.md
cat $ARTIFACTS_DIR/implementation.md
cat $ARTIFACTS_DIR/validation.md
cat $ARTIFACTS_DIR/hardware-e2e.md
```

Extract: plan title/summary, branch, files changed, tests written, validation
results, hardware results, deviations.

Check for a PR template at `.github/pull_request_template.md`,
`.github/PULL_REQUEST_TEMPLATE.md`, or `docs/PULL_REQUEST_TEMPLATE.md`; use it
if present. Check for an existing PR:

```bash
gh pr list --head $(git branch --show-current) --json number,url,state
```

---

## Phase 2: COMMIT + PUSH

```bash
git status --porcelain
```

Stage **only** the implementation files by name — never `git add -A`/`git add .`/
`git add -u`. Never stage: `.pr-body.md`, `pr-body.md`, `*.scratch.md`,
`*.tmp.md`, `review/`, `*-report.md` at the root, or anything under
`$ARTIFACTS_DIR`. Verify with `git diff --cached --name-only`.

Commit:

```bash
git commit -m "{summary}

- {key change 1}
- {key change 2}

{Implements phase N of smart-irrigation development plan / Evaluation first deliverable}"
```

```bash
git push origin HEAD
```

---

## Phase 3: CREATE/UPDATE PR

Default body (or fill the project template):

```markdown
## Summary

{plan summary — what this feature/phase delivers}

## Changes

| File | Action | Description |
|------|--------|-------------|

## Tests

- {test files + cases}

## Validation

{from validation.md — condensed table}

- [x] `bazel build //...`
- [x] BUILD completeness (every changed .py in a target)
- [x] ruff / mypy (changed files)
- [x] MicroPython syntax + vendored integrity
- [x] Unit tests ({N})

## Hardware E2E

{Insert the FULL contents of hardware-e2e.md — devices + results + reasons.
If it reports SKIPPED/UNVERIFIABLE, include that warning verbatim and
prominently. Never omit this section, never claim results that don't exist.}

## Development Plan

- **Phase**: {n / evaluation}
- **Evaluation verdict applied**: {fuzzy | model-based | state machine | PID | n/a}
- **Blueprint deviations**: {none / list with reason}

## Implementation Notes

### Deviations from Plan
{...}

---

**Plan**: `{path}`
**Workflow ID**: `$WORKFLOW_ID`
```

Create or update:

```bash
gh pr create --title "{title}" --body-file $ARTIFACTS_DIR/pr-body.md --base $BASE_BRANCH
# or
gh pr edit {pr-number} --body-file $ARTIFACTS_DIR/pr-body.md
gh pr ready {pr-number} 2>/dev/null || true
```

Body files MUST live at `$ARTIFACTS_DIR/pr-body.md` or `/tmp/` — never in the
worktree.

Capture identifiers:

```bash
PR_NUMBER=$(gh pr view --json number -q '.number')
PR_URL=$(gh pr view --json url -q '.url')
echo "$PR_NUMBER" > "$ARTIFACTS_DIR/.pr-number"
echo "$PR_URL" > "$ARTIFACTS_DIR/.pr-url"
```

---

## Phase 4: OUTPUT

```markdown
## PR Ready ✅

**PR**: #{N} {url}
**Branch**: {branch}
**Hardware E2E**: {status from artifact}
Next: review pipeline.
```

## Success Criteria

- **GATE_ENFORCED**: PR blocked unless `hardware-e2e.md` exists
- **EVIDENCE_INCLUDED**: full hardware table/warning in the PR body
- **CLEAN_COMMIT**: only implementation files staged
- **READY**: PR open, not draft, correct base branch
- **IDs_CAPTURED**: `.pr-number` / `.pr-url` written
