---
description: Simplify code changed in this PR \u2014 implements fixes directly, commits, and pushes. LMAO-adapted: Bazel validation, hardware safety, runfiles-workaround preservation.
argument-hint: (none - operates on the current branch diff against $BASE_BRANCH)
---

# Simplify Changed Code

---

## ⚠️ LMAO Project Rules (MANDATORY — from AGENTS.md)

**Hardware safety — violating these can brick physical devices:**

1. **NEVER run esptool on the Cardputer** (`/dev/ttyACM*`) or flash the RNode via esptool (`/dev/ttyUSB*`). Flash the Cardputer ONLY via `bazel run //cardputer_client:flash`.
2. **Leave the production Cardputer running** with restored config/DEST_HASH after any test session.

**Build rules:**

- **Bazel is the canonical build.** Every `*.py` file must belong to a BUILD target.
- Vendored code is lint-exempt — never "fix" style in `cardputer_client/lib/urns/**` or `rnode_firmware/esptool.py`.

**⛔ DO NOT "SIMPLIFY AWAY" BAZEL WORKAROUNDS (issue #87 regression):**

These look redundant but are load-bearing under Bazel runfiles/sandboxing. Removing any of them breaks tests at module load:

- `sys.path.insert(...)` / `sys.path` bootstraps in `tests/**` (e.g. the `e2e_helpers` import bootstrap in `tests/e2e/test_cardputer_lora_e2e.py`)
- `imports = ["."]` and `conftest.py` in `srcs` in `tests/BUILD`
- Comments mentioning "Bazel sandbox" / "Bazel runfiles" — they mark non-obvious requirements
- MicroPython compatibility shims in `cardputer_client/**` (e.g. `ucontextlib`, try/except import fallbacks) — this code runs on-device, CPython idioms do not apply

When in doubt, LEAVE the code as-is. Simplification must never change behavior.

---

## IMPORTANT: Output Behavior

**Your output will be posted as a GitHub comment.** Keep working output minimal:
- Do NOT narrate each step
- Do NOT output verbose progress updates
- Only output the final structured report at the end

---

## Your Mission

Review ALL code changed on this branch and implement simplifications directly. You are not advisory \u2014 you edit files, validate, commit, and push.

## Scope

**Only code changed in this PR** \u2014 run \`git diff $BASE_BRANCH...HEAD --name-only\` to get the file list. Do not touch unrelated files.

## What to Simplify

| Opportunity | What to Look For |
|-------------|------------------|
| **Unnecessary complexity** | Deep nesting, convoluted logic paths |
| **Redundant code** | Duplicated logic, unused variables/imports |
| **Over-abstraction** | Abstractions that obscure rather than clarify |
| **Poor naming** | Unclear variable/function names |
| **Nested ternaries** | Multiple conditions in ternary chains \u2014 use if/else |
| **Dense one-liners** | Compact code that sacrifices readability |
| **Obvious comments** | Comments that describe what code clearly shows |
| **Inconsistent patterns** | Code that doesn't follow project conventions (read AGENTS.md) |

## Rules

- **Preserve exact functionality** \u2014 simplification must not change behavior
- **Clarity over brevity** \u2014 readable beats compact
- **No speculative refactors** \u2014 only simplify what's obviously improvable
- **Follow project conventions** \u2014 read AGENTS.md and README.md before making changes
- **Never touch the Bazel/MicroPython workarounds** listed at the top of this document
- **Small, obvious changes** \u2014 each simplification should be self-evidently correct

## Process

### Phase 1: ANALYZE

1. Read AGENTS.md for project conventions
2. Get changed files: \`git diff $BASE_BRANCH...HEAD --name-only\`
3. Read each changed file
4. Identify simplification opportunities per file

### Phase 2: IMPLEMENT

For each simplification:
1. Edit the file
2. Run \`bazel build //...\` \u2014 if it fails, revert that change
3. Run \`ruff check <changed files>\` \u2014 if it fails, fix or revert

**Track every path you edit.** You will need this list in Phase 3 to stage only the files you touched.

### Phase 3: VALIDATE & COMMIT

1. Run the FULL LMAO unit gate (not just a subset — the #87 regression passed partial checks):
   \`\`\`bash
   bazel build //...
   ruff check <changed-files> && ruff format --check <changed-files>
   mypy <changed-files>
   bazel test //tests:all --test_tag_filters=-requires_hardware --test_output=errors
   \`\`\`
   If anything fails, fix or revert before committing.
2. If simplifications were applied, stage **only** the files you edited in Phase 2 \u2014 never \`git add -A\`, \`git add .\`, or \`git add -u\`:
   \`\`\`bash
   # Stage by name, using the list you tracked in Phase 2
   git add path/to/file1.ts path/to/file2.ts
   # Verify nothing else snuck in
   git status --porcelain
   \`\`\`
3. **Never stage** report, scratch, or PR-body artifacts, even if they show up as untracked or modified in the worktree:
   - Anything under \`$ARTIFACTS_DIR\` (the artifacts directory normally lives outside the worktree, but copies/symlinks may exist)
   - \`review/\`, \`simplify-report.md\`, \`*-report.md\` at the repo root
   - \`.pr-body.md\`, \`pr-body.md\`, \`*.scratch.md\`, \`*.tmp.md\`
   - If \`git status --porcelain\` shows files you don't recognize as part of your simplifications, leave them unstaged
4. Commit and push only the staged source edits:
   \`\`\`bash
   git commit -m "simplify: reduce complexity in changed files"
   git push
   \`\`\`
5. If no simplifications were applied, skip the commit entirely

### Phase 4: REPORT

Write report to \`$ARTIFACTS_DIR/review/simplify-report.md\` and output:

\`\`\`markdown
## Code Simplification Report

### Changes Made

#### 1. [Brief Title]
**File**: \`path/to/file.ts:45-60\`
**Type**: Reduced nesting / Improved naming / Removed redundancy / etc.
**Before**: [snippet]
**After**: [snippet]

---

### Summary

| Metric | Value |
|--------|-------|
| Files analyzed | X |
| Simplifications applied | Y |
| Net line change | -N lines |
| Validation | PASS / FAIL |

### No Changes Needed
(If nothing to simplify, say so \u2014 "Code is already clean. No simplifications applied.")
\`\`\`
