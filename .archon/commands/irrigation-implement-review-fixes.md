---
description: Fix smart-irrigation review findings (consolidated + control/safety), validate, commit, push so the final gate chain re-runs.
argument-hint: (none - reads review artifacts)
---

# Smart Irrigation — Implement Review Fixes

**Workflow ID**: $WORKFLOW_ID

## ⚠️ Project rules (MANDATORY)

Read and obey `smart_irrigation/AGENTS.md` and `/home/pondycrane/LMAO/AGENTS.md`.
Never esptool; no hardware access; leave the production Cardputer running.
Pump safety overrides are hard-coded and must remain fail-off.

---

## Your Mission

Apply the review findings — **especially the control/safety findings** — and
validate every fix. Any commit here invalidates the gate marker, so the
workflow re-runs the full gate chain after you.

**Output artifacts**: `$ARTIFACTS_DIR/review/fix-report.md`, code changes
committed + pushed.

---

## Phase -1: LOCATE THE RUN TREE (MANDATORY)

```bash
git worktree list --porcelain | grep -E "^(worktree|branch)"
```

Use this run's worktree (`~/.archon/workspaces/*/worktrees/archon/task-*`);
fall back to the current checkout for `--no-worktree` runs.

```bash
WT=/absolute/path/to/run-tree
cd "$WT"
```

---

## Phase 1: LOAD FINDINGS

```bash
PR_NUMBER=$(cat $ARTIFACTS_DIR/.pr-number)
cat $ARTIFACTS_DIR/review/consolidated-review.md
cat $ARTIFACTS_DIR/review/control-safety-findings.md
# if details are needed:
cat $ARTIFACTS_DIR/review/code-review-findings.md 2>/dev/null
cat $ARTIFACTS_DIR/review/error-handling-findings.md 2>/dev/null
cat $ARTIFACTS_DIR/review/test-coverage-findings.md 2>/dev/null
cat $ARTIFACTS_DIR/review/comment-quality-findings.md 2>/dev/null
cat $ARTIFACTS_DIR/review/docs-impact-findings.md 2>/dev/null
```

Prioritize:

1. **ALL CRITICAL/HIGH from control-safety findings** — mandatory.
2. All CRITICAL/HIGH from the consolidated review — mandatory.
3. MEDIUM where the fix is low-risk and in scope.
4. LOW at discretion; don't churn unrelated code.

If a finding is wrong/out of scope, record why in the report instead of
"fixing" it — never weaken a safety override to satisfy a review comment.

---

## Phase 2: FIX

For each accepted finding:

1. Confirm the finding against the current code (`file:line`).
2. Fix the root cause, mirroring existing patterns (`cardputer_client/`).
3. Preserve everything the rules mark load-bearing: BUILD wiring, runfiles
   workarounds (`sys.path` bootstraps, `imports = ["."]`), MicroPython import
   guards, vendored-copy byte-identity, safety overrides.
4. Add/extend a host test for the fixed behavior (especially safety/override
   paths). Wire new test files into BUILD (`py_test`) — issue #87.
5. Validate after each fix:
   ```bash
   bazel build //...
   bazel test //tests:<affected> --test_output=errors
   ruff check <changed> && ruff format --check <changed> && mypy <changed>
   ```
6. Re-run the affected irrigation checks from `irrigation-validate` (5.2
   vendored integrity, 5.3 control evidence, 5.4 protocol round-trip) when the
   fix touches those areas.

Do not touch hardware; hardware-sensitive fixes must be verifiable in the
final hardware gate or explicitly listed for it.

---

## Phase 3: COMMIT + PUSH

Stage only the files you changed, by name (never `git add -A`/`.`/`-u`). Never
stage review artifacts, `.pr-body.md`, `*.scratch.md`, or anything under
`$ARTIFACTS_DIR`. Verify with `git diff --cached --name-only`.

```bash
git commit -m "fix(review): address control/safety and review findings

- {fix 1}
- {fix 2}"
git push origin HEAD
```

---

## Phase 4: REPORT

Write `$ARTIFACTS_DIR/review/fix-report.md`:

```markdown
# Review Fix Report

**PR**: #{N}
**Workflow ID**: $WORKFLOW_ID

## Fixed

| Finding | Severity | File(s) | Fix | Test |
|---------|----------|---------|-----|------|

## Not Fixed (with reason)

| Finding | Severity | Reason | Follow-up |
|---------|----------|--------|-----------|

## Validation

| Check | Result |
|-------|--------|
| bazel build //... | ✅ |
| affected unit tests | ✅ ({N}) |
| ruff + mypy | ✅ |
| irrigation re-checks | ✅/N/A |

## Commits

{sha — message}
```

Comment on the PR briefly:

```bash
gh pr comment "$PR_NUMBER" --body "Review fixes applied: {summary}. Validation green. Full gate chain re-runs next (the commit invalidates the fast-pass marker)."
```

---

## Phase 5: OUTPUT

```markdown
## Review Fixes {✅ APPLIED | PARTIAL}

**Fixed**: {n} findings ({c} critical, {h} high) · **Deferred**: {m}
**Validated**: bazel build ✅ · tests ✅ ({N})
**Artifact**: `$ARTIFACTS_DIR/review/fix-report.md`
Next: final gate chain (validate → hardware E2E → production health).
```

## Success Criteria

- **SAFETY_FIXED**: every CRITICAL/HIGH control-safety finding fixed or explicitly justified
- **VALIDATED**: build + affected tests green after fixes
- **BUILD_WIRED**: any new test file has a `py_test` target
- **COMMITTED_PUSHED**: fixes pushed so the final gate chain runs on the new HEAD
- **REPORTED**: `fix-report.md` complete + PR comment posted
- **NO_HARDWARE_ACCESS**: no device touched
