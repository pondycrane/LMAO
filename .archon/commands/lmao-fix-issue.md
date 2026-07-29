---
description: Implement a fix from investigation artifact - code changes, validation, and commit (no PR). LMAO-adapted: Bazel build/test, hardware safety rules.
argument-hint: <issue-number|artifact-path>
---

# Fix Issue

**Input**: $ARGUMENTS

---

## ⚠️ LMAO Project Rules (MANDATORY — from AGENTS.md)

**Hardware safety — violating these can brick physical devices:**

1. **NEVER run esptool on the Cardputer** (`/dev/ttyACM*`) — no flashing, no `chip_id`, no probing. esptool kills the USB-Serial-JTAG interface; recovery needs a physical USB unplug/replug. The Cardputer is flashed ONLY via `bazel run //cardputer_client:flash` (MicroPython raw REPL).
2. **NEVER flash the RNode via esptool** (`/dev/ttyUSB*`) — the only supported method is the web flasher at https://flasher.rnode.network/. An interrupted esptool flash bricks the device.
3. **Leave the production Cardputer running** — after any flash/test session it must end up with the production client files, the server's `DEST_HASH`, and a running main loop (sends `Hello from Cardputer` every ~60 s).
4. Radio parameters (868 MHz, BW 125 kHz, SF 7, CR 4:5, preamble 24, syncword 0x1424) must stay in sync between server and client — don't change them unless the task requires it.

**Build rules:**

- **Bazel is the canonical build** (v7.4.1 via bazelisk). Every new `*.py` file MUST belong to a BUILD target — a test file without a `py_test` target silently never runs (issue #87).
- Lint: `ruff check` / `ruff format --check` (config `ruff.toml`). Type-check: `mypy` (config `mypy.ini`). Run them on changed files only — master carries pre-existing debt in vendored/standalone files.
- Vendored code is lint-exempt — never "fix" style in `cardputer_client/lib/urns/**` or `rnode_firmware/esptool.py`.
- Unit gate: `bazel test //tests:all --test_tag_filters=-requires_hardware` must pass.
- Hardware E2E (mandatory per AGENTS.md when devices attached): `bazel test //tests:test_cardputer_e2e --test_output=all` and `bazel test //tests:test_cardputer_lora_e2e --test_output=all` — run by the `lmao-hardware-e2e` gate, not by this command.

---

## Your Mission

Execute the implementation plan from \`/investigate-issue\`:

1. Load and validate the artifact
2. Ensure git state is correct
3. Discover and install dependencies in the worktree
4. Implement the changes exactly as specified
5. Run validation
6. Commit changes
7. Write implementation report

**Golden Rule**: Follow the artifact. If something seems wrong, validate it first - don't silently deviate.

---

## Phase 1: LOAD - Get the Artifact

### 1.1 Find Investigation Artifact

Look for the investigation artifact from the previous step:

\`\`\`bash
# Check for artifact in workflow runs directory
ls $ARTIFACTS_DIR/investigation.md
\`\`\`

**If input is a specific path**, use that path directly.

### 1.2 Load and Parse Artifact

\`\`\`bash
cat {artifact-path}
\`\`\`

**Extract from artifact:**
- Issue number and title
- Type (BUG/ENHANCEMENT/etc)
- Files to modify (with line numbers)
- Implementation steps
- Validation commands
- Test cases to add

### 1.3 Validate Artifact Exists

**If artifact not found:**
\`\`\`
\u274C Investigation artifact not found at $ARTIFACTS_DIR/investigation.md

Run \`/investigate-issue {number}\` first to create the implementation plan.
\`\`\`

**PHASE_1_CHECKPOINT:**
- [ ] Artifact found and loaded
- [ ] Key sections parsed (files, steps, validation)
- [ ] Issue number extracted (if applicable)

---

## Phase 2: VALIDATE - Sanity Check

### 2.1 Verify Plan Accuracy

For each file mentioned in the artifact:
- Read the actual current code
- Compare to what artifact expects
- Check if the "current code" snippets match reality

**If significant drift detected:**
\`\`\`
\u26A0\uFE0F Code has changed since investigation:

File: src/x.ts:45
- Artifact expected: {snippet}
- Actual code: {different snippet}

Options:
1. Re-run /investigate-issue to get fresh analysis
2. Proceed carefully with manual adjustments
\`\`\`

### 2.2 Confirm Approach Makes Sense

Ask yourself:
- Does the proposed fix actually address the root cause?
- Are there obvious problems with the approach?
- Has something changed that invalidates the plan?

**If plan seems wrong:**
- STOP
- Explain what's wrong
- Suggest re-investigation

**PHASE_2_CHECKPOINT:**
- [ ] Artifact matches current codebase state
- [ ] Approach still makes sense
- [ ] No blocking issues identified

---

## Phase 3: GIT-CHECK - Ensure Correct State

### 3.1 Check Current Git State

\`\`\`bash
# What branch are we on?
git branch --show-current

# Are we in a worktree?
git rev-parse --show-toplevel
git worktree list

# Is working directory clean?
git status --porcelain

# Are we up to date with remote?
git fetch origin
git status
\`\`\`

### 3.2 Decision Tree

\`\`\`text
\u250C\u2500 IN WORKTREE?
\u2502  \u2514\u2500 YES \u2192 Use current branch AS-IS. Do NOT switch branches. Do NOT create
\u2502           new branches. The isolation system has already set up the correct
\u2502           branch; any deviation operates on the wrong code.
\u2502           Log: "Using worktree at {path} on branch {branch}"
\u2502
\u251C\u2500 ON $BASE_BRANCH? (main, master, or configured base branch)
\u2502  \u2514\u2500 Q: Working directory clean?
\u2502     \u251C\u2500 YES \u2192 Create branch: fix/issue-{number}-{slug}
\u2502     \u2502        git checkout -b fix/issue-{number}-{slug}
\u2502     \u2502        (only applies outside a worktree \u2014 e.g., manual CLI usage)
\u2502     \u2514\u2500 NO  \u2192 STOP: "Uncommitted changes on $BASE_BRANCH.
\u2502              Please commit or stash before proceeding."
\u2502
\u251C\u2500 ON OTHER BRANCH?
\u2502  \u2514\u2500 Use it AS-IS (assume it was set up for this work).
\u2502     Do NOT switch to another branch (e.g., one shown by \`git branch\` but
\u2502     not currently checked out).
\u2502     If branch name doesn't contain issue number:
\u2502       Warn: "Branch '{name}' may not be for issue #{number}"
\u2502
\u2514\u2500 DIRTY STATE?
   \u2514\u2500 STOP: "Uncommitted changes. Please commit or stash first."
\`\`\`

### 3.3 Ensure Up-to-Date

\`\`\`bash
# If branch tracks remote
git pull --rebase origin $BASE_BRANCH 2>/dev/null || git pull origin $BASE_BRANCH
\`\`\`

**PHASE_3_CHECKPOINT:**
- [ ] Git state is clean and correct
- [ ] On appropriate branch (created or existing)
- [ ] Up to date with base branch

---

## Phase 4: DEPENDENCIES - Bazel Handles Them

### 4.1 Bazel Resolves Everything

LMAO's dependencies are pinned in `lmao_server/requirements_lock.txt` and fetched by Bazel (`@lmao_pip`). There is no separate install step — a build pulls what it needs:

\`\`\`bash
bazel build //...
\`\`\`

This also regenerates the protobuf stubs (`proto/*_pb2.py`).

### 4.2 Failure Handling

If the build fails on dependency fetching (network, lock mismatch), STOP and report the error. Do not proceed to validation with missing dependencies.

**PHASE_4_CHECKPOINT:**
- [ ] `bazel build //...` exits 0

---

## Phase 5: IMPLEMENT - Make Changes

### 5.1 Execute Each Step

For each step in the artifact's Implementation Plan:

1. **Read the target file** - understand current state
2. **Make the change** - exactly as specified
3. **Verify it builds** - \`bazel build //...\` (plus \`ruff check <changed files>\`)

**New `*.py` files**: immediately add them to the appropriate `BUILD` target (`py_library`/`py_test`/`py_binary`) — a file not referenced by Bazel is never built or tested (issue #87 regression class). New test files in `tests/` need their own `py_test` (mirror neighboring targets: `conftest.py` in `srcs` and `imports = ["."]` where siblings use them).

### 5.2 Implementation Rules

**DO:**
- Follow artifact steps in order
- Match existing code style exactly
- Copy patterns from "Patterns to Follow" section
- Add tests as specified

**DON'T:**
- Refactor unrelated code
- Add "improvements" not in the plan
- Change formatting of untouched lines
- Deviate from the artifact without noting it

### 5.3 Handle Each File Type

**For UPDATE files:**
- Read current content
- Find the exact lines mentioned
- Make the specified change
- Preserve surrounding code

**For CREATE files:**
- Use patterns from artifact
- Follow existing file structure conventions
- Include all specified content

**For test files:**
- Add test cases as specified
- Follow existing test patterns
- Ensure tests actually test the fix

### 5.4 Track Deviations

If you must deviate from the artifact:
- Note what changed and why
- Include in implementation report

**PHASE_5_CHECKPOINT:**
- [ ] All steps from artifact executed
- [ ] Types compile after each change
- [ ] Tests added as specified
- [ ] Any deviations documented

---

## Phase 6: VERIFY - Run Validation

### 6.1 Run Artifact Validation Commands

Execute each command from the artifact's Validation section. For LMAO these are always the Bazel-native checks (ignore any npm/bun-style commands copied into a stale artifact — they do not exist here):

\`\`\`bash
bazel build //...
ruff check <changed-files>
mypy <changed-files>
bazel test //tests:all --test_tag_filters=-requires_hardware --test_output=errors
\`\`\`

### 6.2 Check Results

**All must pass before proceeding.**

If failures:
1. Analyze what's wrong
2. Fix the issue
3. Re-run validation
4. Note any fixes in implementation report

### 6.3 Manual Verification (if specified)

Execute any manual verification steps from the artifact.

**PHASE_6_CHECKPOINT:**
- [ ] `bazel build //...` passes
- [ ] `bazel test //tests:all --test_tag_filters=-requires_hardware` passes
- [ ] `ruff check` / `mypy` pass on changed files
- [ ] Every new/changed `.py` belongs to a BUILD target
- [ ] Manual verification complete (if applicable)

---

## Phase 7: COMMIT - Save Changes

### 7.1 Stage Changes

Stage **only** the files you actually edited \u2014 never \`git add -A\`, \`git add .\`, or \`git add -u\`. List them by name:

\`\`\`bash
git add path/to/file1 path/to/file2 ...
git status --porcelain  # verify nothing scratch/review/PR-body is staged
\`\`\`

**Never stage**:

- \`.pr-body.md\`, \`pr-body.md\`, \`*.scratch.md\`, \`*.tmp.md\`
- \`review/\`, \`*-report.md\` at the repo root
- Anything under \`$ARTIFACTS_DIR\`

### 7.2 Write Commit Message

**Format:**
\`\`\`
Fix: {brief description} (#{issue-number})

{Problem statement from artifact - 1-2 sentences}

Changes:
- {Change 1 from artifact}
- {Change 2 from artifact}
- Added test for {case}

Fixes #{issue-number}
\`\`\`

**Commit:**
\`\`\`bash
git commit -m "$(cat <<'EOF'
Fix: {title} (#{number})

{problem statement}

Changes:
- {change 1}
- {change 2}

Fixes #{number}
EOF
)"
\`\`\`

**PHASE_7_CHECKPOINT:**
- [ ] All changes committed
- [ ] Commit message references issue

---

## Phase 8: WRITE - Implementation Report

### 8.1 Write Implementation Artifact

Write to \`$ARTIFACTS_DIR/implementation.md\`:

\`\`\`markdown
# Implementation Report

**Issue**: #{number}
**Generated**: {YYYY-MM-DD HH:MM}
**Workflow ID**: $WORKFLOW_ID

---

## Tasks Completed

| # | Task | File | Status |
|---|------|------|--------|
| 1 | {task} | \`src/x.ts\` | \u2705 |
| 2 | {task} | \`src/x.test.ts\` | \u2705 |

---

## Files Changed

| File | Action | Lines |
|------|--------|-------|
| \`src/x.ts\` | UPDATE | +{N}/-{M} |
| \`src/x.test.ts\` | CREATE | +{N} |

---

## Deviations from Investigation

{If none: "Implementation matched the investigation exactly."}

{If any:}
### Deviation 1: {title}

**Expected**: {from investigation}
**Actual**: {what was done}
**Reason**: {why}

---

## Validation Results

| Check | Result |
|-------|--------|
| Bazel build | \u2705 |
| Unit tests | \u2705 ({N} passed) |
| ruff / mypy | \u2705 |
| BUILD completeness | \u2705 |
\`\`\`

**PHASE_8_CHECKPOINT:**
- [ ] Implementation artifact written

---

## Phase 9: OUTPUT - Report to User

Skip archiving - artifacts remain in place for review workflow to access.

---

\`\`\`markdown
## Implementation Complete

**Issue**: #{number} - {title}
**Branch**: \`{branch-name}\`

### Changes Made

| File | Change |
|------|--------|
| \`src/x.ts\` | {description} |
| \`src/x.test.ts\` | Added test |

### Validation

| Check | Result |
|-------|--------|
| Bazel build | \u2705 Pass |
| Unit tests | \u2705 Pass |
| ruff / mypy | \u2705 Pass |

### Artifacts

- \uD83D\uDCC4 Investigation: \`$ARTIFACTS_DIR/investigation.md\`
- \uD83D\uDCC4 Implementation: \`$ARTIFACTS_DIR/implementation.md\`

### Next Step

Proceeding to PR creation...
\`\`\`

---

## Handling Edge Cases

### Artifact is outdated
- Warn user about drift
- Suggest re-running \`/investigate-issue\`
- Can proceed with caution if changes are minor

### Tests fail after implementation
- Debug the failure
- Fix the code (not the test, unless test is wrong)
- Re-run validation
- Note the additional fix in implementation report

### Merge conflicts during rebase
- Resolve conflicts
- Re-run full validation
- Note conflict resolution in implementation report

### Already on a branch with changes
- Use the existing branch
- Warn if branch name doesn't match issue
- Don't create a new branch

### In a worktree
- Use it as-is
- Assume it was created for this purpose
- Log that worktree is being used

---

## Success Criteria

- **PLAN_EXECUTED**: All investigation steps completed
- **VALIDATION_PASSED**: All checks green
- **CHANGES_COMMITTED**: All changes committed to branch
- **IMPLEMENTATION_ARTIFACT**: Written to $ARTIFACTS_DIR/
- **READY_FOR_PR**: Workflow continues to PR creation
