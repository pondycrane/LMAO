---
description: Verify plan research is still valid - check patterns exist, code hasn't drifted. LMAO-adapted: Bazel validation commands.
argument-hint: (no arguments - reads from workflow artifacts)
---

# Confirm Plan Research

**Workflow ID**: $WORKFLOW_ID

---

## ⚠️ LMAO Project Rules (MANDATORY — from AGENTS.md)

- **NEVER run esptool on the Cardputer** (`/dev/ttyACM*`) or flash the RNode via esptool (`/dev/ttyUSB*`). The Cardputer is flashed ONLY via `bazel run //cardputer_client:flash`; the RNode only via https://flasher.rnode.network/.
- **Bazel is the canonical build.** Every new `*.py` file must belong to a BUILD target — a test file without a `py_test` target silently never runs (issue #87).
- Unit gate: `bazel test //tests:all --test_tag_filters=-requires_hardware`.

---

## Your Mission

Verify that the plan's research is still valid before implementation begins.

Plans can become stale:
- Files may have been renamed or moved
- Code patterns may have changed
- APIs may have been updated

**This step does NOT implement anything** - it only validates the plan is still accurate.

---

## Phase 1: LOAD - Read Context Artifact

### 1.1 Load Plan Context

\`\`\`bash
cat $ARTIFACTS_DIR/plan-context.md
\`\`\`

If not found, STOP with error:
\`\`\`
\u274C Plan context not found at $ARTIFACTS_DIR/plan-context.md

Run archon-plan-setup first.
\`\`\`

### 1.2 Extract Verification Targets

From the context, identify:

1. **Patterns to Mirror** - Files and line ranges to verify
2. **Files to Change** - Files that will be created/updated
3. **Validation Commands** - Commands that should work

**PHASE_1_CHECKPOINT:**

- [ ] Context artifact loaded
- [ ] Patterns to verify extracted
- [ ] Files to change identified

---

## Phase 2: VERIFY - Check Patterns Exist

### 2.1 Verify Pattern Files

For each file in "Patterns to Mirror":

1. Check if file exists:
   \`\`\`bash
   test -f {file-path} && echo "EXISTS" || echo "MISSING"
   \`\`\`

2. If exists, read the referenced lines:
   \`\`\`bash
   sed -n '{start},{end}p' {file-path}
   \`\`\`

3. Compare with what the plan expected (if plan included code snippets)

### 2.2 Document Findings

For each pattern file:

| File | Status | Notes |
|------|--------|-------|
| \`src/adapters/telegram.ts\` | \u2705 EXISTS | Lines 11-23 match expected pattern |
| \`src/types/index.ts\` | \u2705 EXISTS | Interface still present |
| \`src/old-file.ts\` | \u274C MISSING | File was renamed/deleted |
| \`src/changed.ts\` | \u26A0\uFE0F DRIFTED | Code structure changed significantly |

### 2.3 Severity Assessment

| Finding | Severity | Action |
|---------|----------|--------|
| File exists, code matches | \u2705 OK | Proceed |
| File exists, minor differences | \u26A0\uFE0F WARNING | Note in artifact, proceed with caution |
| File exists, major drift | \uD83D\uDFE0 CONCERN | Flag for review, may need plan update |
| File missing | \u274C BLOCKER | Stop, plan needs revision |

**PHASE_2_CHECKPOINT:**

- [ ] All pattern files checked
- [ ] Findings documented
- [ ] Severity assessed

---

## Phase 3: VERIFY - Check Target Locations

### 3.1 Check Files to Create

For each file marked CREATE:

1. Verify it doesn't already exist (would be unexpected):
   \`\`\`bash
   test -f {file-path} && echo "ALREADY EXISTS" || echo "OK - will create"
   \`\`\`

2. Verify parent directory exists or can be created:
   \`\`\`bash
   dirname {file-path} | xargs test -d && echo "DIR EXISTS" || echo "DIR WILL BE CREATED"
   \`\`\`

### 3.2 Check Files to Update

For each file marked UPDATE:

1. Verify it exists:
   \`\`\`bash
   test -f {file-path} && echo "EXISTS" || echo "MISSING"
   \`\`\`

2. If the plan references specific lines/functions, verify they exist

**PHASE_3_CHECKPOINT:**

- [ ] CREATE targets verified (don't exist yet)
- [ ] UPDATE targets verified (do exist)

---

## Phase 4: VERIFY - Check Validation Commands

### 4.1 Dry Run Validation Commands

Test that the validation commands work (without expecting them to pass):

\`\`\`bash
# Check bazel exists and the workspace loads
bazel query //tests:all >/dev/null 2>&1 && echo "bazel available" || echo "bazel not available"

# Check lint/type tools exist
ruff --version 2>/dev/null || echo "ruff not available"
mypy --version 2>/dev/null || echo "mypy not available"
\`\`\`

### 4.2 Document Command Availability

| Command | Status |
|---------|--------|
| \`bazel build //...\` | \u2705 Available |
| \`ruff check\` | \u2705 Available |
| \`mypy\` | \u2705 Available |
| \`bazel test //tests:all --test_tag_filters=-requires_hardware\` | \u2705 Available |

**PHASE_4_CHECKPOINT:**

- [ ] Validation commands tested
- [ ] All required commands available

---

## Phase 5: ARTIFACT - Write Confirmation

### 5.1 Write Confirmation Artifact

Write to \`$ARTIFACTS_DIR/plan-confirmation.md\`:

\`\`\`markdown
# Plan Confirmation

**Generated**: {YYYY-MM-DD HH:MM}
**Workflow ID**: $WORKFLOW_ID
**Status**: {CONFIRMED | WARNINGS | BLOCKED}

---

## Pattern Verification

| Pattern | File | Status | Notes |
|---------|------|--------|-------|
| Constructor pattern | \`src/adapters/telegram.ts:11-23\` | \u2705 | Matches expected |
| Interface definition | \`src/types/index.ts:49-74\` | \u2705 | Present |
| ... | ... | ... | ... |

**Pattern Summary**: {X} of {Y} patterns verified

---

## Target Files

### Files to Create

| File | Status |
|------|--------|
| \`src/new-file.ts\` | \u2705 Does not exist (ready to create) |

### Files to Update

| File | Status |
|------|--------|
| \`src/existing.ts\` | \u2705 Exists |

---

## Validation Commands

| Command | Available |
|---------|-----------|
| \`bazel build //...\` | \u2705 |
| \`ruff check\` | \u2705 |
| \`mypy\` | \u2705 |
| \`bazel test //tests:all --test_tag_filters=-requires_hardware\` | \u2705 |

---

## Issues Found

{If no issues:}
No issues found. Plan research is valid.

{If issues:}
### Warnings

- **{file}**: {description of drift or concern}

### Blockers

- **{file}**: {description of missing file or critical issue}

---

## Recommendation

{One of:}
- \u2705 **PROCEED**: Plan research is valid, continue to implementation
- \u26A0\uFE0F **PROCEED WITH CAUTION**: Minor drift detected, implementation may need adjustments
- \u274C **STOP**: Critical issues found, plan needs revision

---

## Next Step

{If PROCEED or PROCEED WITH CAUTION:}
Continue to \`archon-implement-tasks\` to execute the plan.

{If STOP:}
Revise the plan to address blockers, then re-run \`archon-plan-setup\`.
\`\`\`

**PHASE_5_CHECKPOINT:**

- [ ] Confirmation artifact written
- [ ] Status clearly indicated
- [ ] Issues documented

---

## Phase 6: OUTPUT - Report to User

### If Confirmed (no blockers):

\`\`\`markdown
## Plan Confirmed \u2705

**Workflow ID**: \`$WORKFLOW_ID\`
**Status**: Ready for implementation

### Verification Summary

| Check | Result |
|-------|--------|
| Pattern files | \u2705 {X}/{Y} verified |
| Target files | \u2705 Ready |
| Validation commands | \u2705 Available |

{If warnings:}
### Warnings

- {warning 1}
- {warning 2}

These are minor and shouldn't block implementation.

### Artifact

Confirmation written to: \`$ARTIFACTS_DIR/plan-confirmation.md\`

### Next Step

Proceed to \`archon-implement-tasks\` to execute the plan.
\`\`\`

### If Blocked:

\`\`\`markdown
## Plan Blocked \u274C

**Workflow ID**: \`$WORKFLOW_ID\`
**Status**: Cannot proceed

### Blockers Found

1. **{file}**: {description}
2. **{file}**: {description}

### Required Action

The plan references files or patterns that no longer exist. Options:

1. **Update the plan** to reflect current codebase state
2. **Restore missing files** if they were accidentally deleted
3. **Re-run planning** with \`/archon-plan\` to generate a fresh plan

### Artifact

Details written to: \`$ARTIFACTS_DIR/plan-confirmation.md\`
\`\`\`

---

## Success Criteria

- **PATTERNS_VERIFIED**: All pattern files exist and are reasonably similar
- **TARGETS_VALID**: CREATE files don't exist, UPDATE files do exist
- **COMMANDS_AVAILABLE**: Validation commands can be run
- **ARTIFACT_WRITTEN**: Confirmation artifact created with clear status
