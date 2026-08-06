---
description: Commit changes, create PR with template + mandatory hardware E2E evidence, mark ready for review. LMAO-adapted.
argument-hint: (no arguments - reads from workflow artifacts)
---

# Finalize Pull Request

**Workflow ID**: $WORKFLOW_ID

---

## ⚠️ LMAO Project Rules (MANDATORY — from AGENTS.md)

- **NEVER run esptool on the Cardputer** (`/dev/ttyACM*`) or flash the RNode via esptool (`/dev/ttyUSB*`).
- The PR body MUST include the hardware E2E evidence from `$ARTIFACTS_DIR/hardware-e2e.md` (results table, or the loud SKIPPED warning if no devices were attached) — a PR without this section is incomplete per AGENTS.md.

---

## Your Mission

Finalize the implementation and create the PR:
1. Commit all changes
2. Push to remote
3. Create PR using project's template (if exists)
4. Mark PR as ready for review

---

## Phase 0: HARDWARE E2E GATE (MANDATORY — do this before anything else)

<!-- Mirror of the create-pr gate in .archon/workflows/lmao-fix-issue.yaml; update both -->

```bash
if [ ! -f "$ARTIFACTS_DIR/hardware-e2e.md" ]; then
  echo "FATAL: $ARTIFACTS_DIR/hardware-e2e.md does not exist —"
  echo "the mandatory AGENTS.md hardware E2E gate did not produce results."
  echo "PR creation is blocked. Re-run the gate chain manually:"
  echo "  bazel test //tests:test_cardputer_e2e //tests:test_cardputer_lora_e2e --test_output=all"
  exit 1
fi
```

This check MUST run before any git add, commit, or push. If it fails,
output the error prominently and STOP — do not proceed to Phase 1.

---

## Phase 1: LOAD - Gather Context

### 1.1 Load Workflow Artifacts

\`\`\`bash
cat $ARTIFACTS_DIR/plan-context.md
cat $ARTIFACTS_DIR/implementation.md
cat $ARTIFACTS_DIR/validation.md
cat $ARTIFACTS_DIR/hardware-e2e.md
\`\`\`

Extract:
- Plan title and summary
- Branch name
- Files changed
- Tests written
- Validation results
- Hardware E2E results table (or SKIPPED warning)
- Deviations from plan (if any)

### 1.2 Check for PR Template

**IMPORTANT**: Always check for the project's PR template first. Look for it at \`.github/pull_request_template.md\`, \`.github/PULL_REQUEST_TEMPLATE.md\`, or \`docs/PULL_REQUEST_TEMPLATE.md\`. Read whichever one exists.

**If template found**: Use it as the structure, fill in **every section** with implementation details.
**If no template**: Use the default format defined in Phase 3.

### 1.3 Check for Existing PR

\`\`\`bash
gh pr list --head $(git branch --show-current) --json number,url,state
\`\`\`

**If PR already exists**: Will update it instead of creating new one.
**If no PR**: Will create new one.

**PHASE_1_CHECKPOINT:**

- [ ] Artifacts loaded
- [ ] Template identified (or using default)
- [ ] Existing PR status known

---

## Phase 2: COMMIT - Stage and Commit Changes

### 2.1 Check Git Status

\`\`\`bash
git status --porcelain
\`\`\`

### 2.2 Stage Changes

Stage **only** the implementation files you actually edited \u2014 never \`git add -A\`, \`git add .\`, or \`git add -u\`. List them by name:

\`\`\`bash
git add path/to/file1 path/to/file2 ...
git status --porcelain  # verify nothing else is staged
\`\`\`

**Never stage** scratch / review / PR-body artifacts, even if they appear in \`git status\`:

- \`.pr-body.md\`, \`pr-body.md\`, \`*.scratch.md\`, \`*.tmp.md\`
- \`review/\`, \`*-report.md\` at the repo root
- Anything under \`$ARTIFACTS_DIR\`

**Review staged files** \u2014 ensure no sensitive files (\`.env\`, credentials) and no scratch artifacts are included:

\`\`\`bash
git diff --cached --name-only
\`\`\`

### 2.3 Create Commit

Create a descriptive commit message:

\`\`\`bash
git commit -m "{summary of implementation}

- {key change 1}
- {key change 2}
- {key change 3}

{If from plan/issue: Implements #{number}}
"
\`\`\`

### 2.4 Push to Remote

\`\`\`bash
git push origin HEAD
\`\`\`

**PHASE_2_CHECKPOINT:**

- [ ] All changes staged
- [ ] No sensitive files included
- [ ] Commit created
- [ ] Pushed to remote

---

## Phase 3: CREATE/UPDATE - Pull Request

### 3.1 Prepare PR Body

**If project has PR template**, fill in each section with implementation details:
- Replace placeholder text with actual content
- Fill in checkboxes based on what was done
- Keep the template's structure intact

**If no template**, use this default format:

\`\`\`markdown
## Summary

{Brief description from plan summary}

## Changes

{From implementation.md "Files Changed" section}

| File | Action | Description |
|------|--------|-------------|
| \`src/x.ts\` | CREATE | {what it does} |
| \`src/y.ts\` | UPDATE | {what changed} |

## Tests

{From implementation.md "Tests Written" section}

- \`src/x.test.ts\` - {test descriptions}
- \`src/y.test.ts\` - {test descriptions}

## Validation

{From validation.md}

- [x] `bazel build //...` passes
- [x] ruff check / format passes (changed files)
- [x] mypy passes (changed files)
- [x] All unit tests pass ({N} tests)
- [x] BUILD completeness — every changed `.py` belongs to a target

## Hardware E2E

{Insert the FULL contents of $ARTIFACTS_DIR/hardware-e2e.md here — devices table +
results table verbatim. If it reports SKIPPED — HARDWARE NOT DETECTED, include that
warning verbatim and prominently; never omit this section.}

## Implementation Notes

{If deviations from plan:}
### Deviations from Plan

{List deviations and reasons}

{If issues encountered:}
### Issues Resolved

{List issues and resolutions}

---

**Plan**: \`{plan-source-path}\`
**Workflow ID**: \`$WORKFLOW_ID\`
\`\`\`

### 3.2 Create or Update PR

**If no PR exists**, create one:

\`\`\`bash
# Write prepared body to file to avoid shell escaping
cat > $ARTIFACTS_DIR/pr-body.md <<'EOF'
{prepared-body}
EOF

gh pr create \\
  --title "{plan-title}" \\
  --body-file $ARTIFACTS_DIR/pr-body.md \\
  --base $BASE_BRANCH
\`\`\`

**If PR already exists**, update it:

\`\`\`bash
gh pr edit {pr-number} --body-file $ARTIFACTS_DIR/pr-body.md
\`\`\`

### 3.3 Ensure Ready for Review

If PR was created as draft, mark ready:

\`\`\`bash
gh pr ready {pr-number} 2>/dev/null || true
\`\`\`

### 3.4 Capture PR Info

\`\`\`bash
gh pr view --json number,url,headRefName,baseRefName
\`\`\`

### 3.5 Write PR Number Registry

Write PR number for downstream review steps:

\`\`\`bash
PR_NUMBER=$(gh pr view --json number -q '.number')
PR_URL=$(gh pr view --json url -q '.url')
echo "$PR_NUMBER" > $ARTIFACTS_DIR/.pr-number
echo "$PR_URL" > $ARTIFACTS_DIR/.pr-url
\`\`\`

**PHASE_3_CHECKPOINT:**

- [ ] PR created or updated
- [ ] PR body uses template (if available)
- [ ] PR ready for review
- [ ] PR URL captured
- [ ] PR number registry written

---

## Phase 4: ARTIFACT - Write PR Ready Status

### 4.1 Write Final Artifact

Write to \`$ARTIFACTS_DIR/pr-ready.md\`:

\`\`\`markdown
# PR Ready for Review

**Generated**: {YYYY-MM-DD HH:MM}
**Workflow ID**: $WORKFLOW_ID

---

## Pull Request

| Field | Value |
|-------|-------|
| **Number** | #{number} |
| **URL** | {url} |
| **Branch** | \`{head}\` \u2192 \`{base}\` |
| **Status** | Ready for Review |

---

## Commit

**Hash**: {commit-sha}
**Message**: {commit-message-first-line}

---

## Files in PR

{From git diff --name-only origin/$BASE_BRANCH}

| File | Status |
|------|--------|
| \`src/x.ts\` | Added |
| \`src/y.ts\` | Modified |

---

## PR Description

{Whether template was used or default format}

- Template used: {yes/no}
- Template path: {path if used}

---

## Next Step

Continue to PR review workflow:
1. \`archon-pr-review-scope\`
2. \`archon-sync-pr-with-main\`
3. Review agents (parallel)
4. \`archon-synthesize-review\`
5. \`archon-implement-review-fixes\`
\`\`\`

**PHASE_4_CHECKPOINT:**

- [ ] PR ready artifact written

---

## Phase 5: OUTPUT - Report Status

\`\`\`markdown
## PR Ready for Review \u2705

**Workflow ID**: \`$WORKFLOW_ID\`

### Pull Request

| Field | Value |
|-------|-------|
| PR | #{number} |
| URL | {url} |
| Branch | \`{branch}\` \u2192 \`{base}\` |
| Status | \uD83D\uDFE2 Ready for Review |

### Commit

\`\`\`
{commit-sha-short} {commit-message-first-line}
\`\`\`

### Files Changed

- {N} files added
- {M} files modified
- {K} files deleted

### Validation Summary

| Check | Status |
|-------|--------|
| Bazel build | \u2705 |
| ruff / mypy | \u2705 |
| Unit tests | \u2705 ({N} passed) |
| Hardware E2E | {✅ / ⚠️ SKIPPED from hardware-e2e.md} |

### Artifact

Status written to: \`$ARTIFACTS_DIR/pr-ready.md\`

### Next Step

Proceeding to comprehensive PR review.
\`\`\`

---

## Error Handling

### Nothing to Commit

If no changes to commit:

\`\`\`markdown
\u2139\uFE0F No changes to commit

All changes were already committed. Proceeding to update PR description.
\`\`\`

### Push Fails

\`\`\`bash
# Try force push if branch was rebased
git push --force-with-lease origin HEAD
\`\`\`

If still fails:
\`\`\`
\u274C Push failed

Check:
1. Branch protection rules
2. Push access to repository
3. Remote branch status: \`git fetch origin && git status\`
\`\`\`

### PR Not Found

\`\`\`
\u274C PR not found: #{number}

The draft PR may have been closed or deleted. Create a new one:
\`gh pr create --title "..." --body "..."\`
\`\`\`

### Template Parsing

If template has complex structure that's hard to fill:
- Use as much of the template as possible
- Add implementation details in relevant sections
- Note at bottom: "Some template sections may need manual completion"

---

## Success Criteria

- **CHANGES_COMMITTED**: All changes in a commit
- **PUSHED**: Branch pushed to remote
- **PR_UPDATED**: PR description reflects implementation
- **PR_READY**: Draft status removed
- **ARTIFACT_WRITTEN**: PR ready artifact created
