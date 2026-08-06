---
description: LMAO validation gate — BUILD completeness, bazel build, ruff, mypy, unit tests (Bazel-native, no hardware)
argument-hint: (no arguments - validates current branch changes vs $BASE_BRANCH)
---

# LMAO Validate

**Workflow ID**: $WORKFLOW_ID

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
- Hardware E2E is NOT part of this command — `lmao-hardware-e2e` runs it next in the workflow.

**Gate-chain safety — violating these can silently skip mandatory tests:**

- **NEVER write `$ARTIFACTS_DIR/.gate-head`** — only `lmao-production-health` owns that marker. Writing it from any other gate node causes downstream hardware E2E to fast-pass without executing (issue #100).

---

## Your Mission

Run the LMAO validation gate on the current branch's changes and fix any failures. This is the Bazel-native replacement for `archon-validate` — there is no `package.json` here; all checks go through Bazel + ruff + mypy.

This is a focused step: run checks, fix issues, repeat until green.

---

## Phase -1: LOCATE THE RUN TREE (MANDATORY — do this before anything else)

Your shell's default directory is the ORIGINAL project checkout, which may be
on a different branch than this run. Every git and bazel command in this
document — including the `.gate-head` marker comparison — MUST run in the
tree that holds this run's branch:

```bash
git worktree list --porcelain | grep -E "^(worktree|branch)"
```

- If a worktree exists for this run's branch (typically under
  `~/.archon/workspaces/*/worktrees/archon/task-*`), use it.
- Otherwise (a `--no-worktree` run), use the current checkout.

Set it once and stick to it for the whole command:

```bash
WT=/absolute/path/to/run-tree
cd "$WT"
```

NEVER mix trees: a `git rev-parse HEAD` in the wrong checkout silently
corrupts the fast-pass marker logic (observed in the issue-#91 live test —
final gates compared the marker against the main checkout's HEAD).

---

## Phase 0: FAST-PASS — Skip If Nothing Changed

```bash
cd "$WT"
if [ -f "$ARTIFACTS_DIR/.gate-head" ] && [ "$(cat "$ARTIFACTS_DIR/.gate-head")" = "$(git rev-parse HEAD)" ] \
   && [ -f "$ARTIFACTS_DIR/validation.md" ]; then
  echo "FAST-PASS"
fi
```

If `FAST-PASS` printed: HEAD is unchanged since the last full gate chain completed, so re-running would re-test identical code. Output exactly:

```markdown
## Validation ✅ (fast-pass — no changes since last gate)
```

and STOP. Do not run any further phases.

If not, continue — and know that review/simplify phases that landed commits invalidate the marker, so the full gate below is mandatory now.

**🚫 DO NOT WRITE `.gate-head`**: This node MUST NOT write or update
`$ARTIFACTS_DIR/.gate-head` under any circumstances. The `.gate-head`
marker is owned EXCLUSIVELY by `lmao-production-health` — it is the
signal that the entire gate chain completed, not just validation.
Writing it prematurely from validate causes downstream hardware gates
to fast-pass without executing (issue #100 regression guard failure).

---

## Phase 1: SCOPE — Changed Python Files

Run inside `"$WT"`:

```bash
CHANGED_PY=$( { git diff --name-only "$BASE_BRANCH"...HEAD -- '*.py'; git ls-files --others --exclude-standard -- '*.py'; } | sort -u | grep -v '^bazel-' || true )
echo "$CHANGED_PY"
```

All lint/type checks below run on `$CHANGED_PY` **only**. Never reformat or "fix" files outside the changed set — master carries pre-existing lint debt in vendored/standalone files (`rnode_firmware/esptool.py`, `diagnose_stack.py`).

If `$CHANGED_PY` is empty, Phases 4–5 are skipped (note it in the artifact); Phases 2, 3 and 6 still run.

---

## Phase 2: BUILD COMPLETENESS — Every Changed .py Belongs To A Target

A new Python file that is not referenced by any Bazel target is silently never built or tested — this is the exact regression class from issue #87 (`tests/test_boot.py` had 8 tests that never ran). Run this check verbatim:

```bash
EXEMPT_RE='(^|/)__init__\.py$|^proto/.*_pb2(_grpc)?\.py$|^rnode_firmware/|^diagnose_stack\.py$|^tests/test_paths\.py$|^tests/e2e/test_debug_paths\.py$|^tools/archon_webhook_relay\.py$'
MISSING=0
for f in $CHANGED_PY; do
  echo "$f" | grep -qE "$EXEMPT_RE" && { echo "EXEMPT:    $f"; continue; }
  pkg_dir=$(dirname "$f")
  while [ "$pkg_dir" != "." ] && [ ! -f "$pkg_dir/BUILD" ]; do pkg_dir=$(dirname "$pkg_dir"); done
  if [ "$pkg_dir" = "." ]; then
    [ -f BUILD ] && label="//:$f" || label=""
  else
    label="//$pkg_dir:${f#$pkg_dir/}"
  fi
  if [ -n "$label" ] && bazel query "$label" >/dev/null 2>&1; then
    echo "COVERED:   $f"
  else
    echo "MISSING:   $f  ← not referenced by any BUILD target"
    MISSING=1
  fi
done
```

The exemption list (do not extend without writing the justification into `$ARTIFACTS_DIR/validation.md`):

| Pattern | Why exempt |
|---------|------------|
| `**/__init__.py` | Package markers — optional in Bazel `py_library` |
| `proto/*_pb2.py`, `proto/*_pb2_grpc.py` | Bazel generates these from `.proto`; checked-in copies only serve the no-Bazel pip flow |
| `rnode_firmware/**` | Vendored firmware tooling, never built |
| `diagnose_stack.py` | Standalone diagnostic script run with plain `python3` |
| `tests/test_paths.py`, `tests/e2e/test_debug_paths.py` | Debug utilities, not tests |
| `tools/archon_webhook_relay.py` | Standalone script run via systemd |

**For every MISSING file**, wire it into the appropriate `BUILD` file (e.g. add a `py_test` for a new `tests/test_*.py` mirroring neighboring targets — remember `conftest.py` in `srcs` + `imports = ["."]` where sibling targets use them), then re-run this phase until nothing is MISSING.

**Record result**: ✅ Pass / ❌ Fail (fixed)

---

## Phase 3: BAZEL BUILD

```bash
bazel build //...
```

Must exit 0. This also regenerates protobuf stubs. Fix BUILD dependency errors before continuing.

**Record result**: ✅ Pass / ❌ Fail (fixed)

---

## Phase 4: LINT

```bash
ruff check $CHANGED_PY
ruff format --check $CHANGED_PY
```

**If `ruff check` fails**: try `ruff check --fix $CHANGED_PY`, then manually fix the rest.
**If format check fails**: run `ruff format $CHANGED_PY`, then re-check.

Config: `ruff.toml` (line-length 100, target py310). Respect `per-file-ignores` — vendored urns code and test idioms are intentionally exempt; do not "clean them up".

**Record result**: ✅ Pass / ❌ Fail (fixed)

---

## Phase 5: TYPE CHECK

```bash
mypy $CHANGED_PY
```

Config: `mypy.ini` (`ignore_missing_imports = True`). Do **not** run `mypy .` on the whole tree — it chokes on Bazel runfiles directories (`flash.runfiles`) and pre-existing package-layout issues.

**Record result**: ✅ Pass / ❌ Fail (fixed)

---

## Phase 6: UNIT TESTS

```bash
bazel test //tests:all --test_tag_filters=-requires_hardware --test_output=errors
```

All tests must pass. Hardware E2E targets (`requires_hardware` tag) are deliberately excluded — `lmao-hardware-e2e` runs them next in the workflow. The K8s test auto-skips when no cluster is reachable.

If a test fails: determine whether the implementation or the test is wrong, fix the root cause, re-run.

**Record result**: ✅ Pass ({N} tests) / ❌ Fail (fixed)

---

## Phase 7: ARTIFACT — Write Validation Results

Write to `$ARTIFACTS_DIR/validation.md`:

```markdown
# LMAO Validation Results

**Generated**: {YYYY-MM-DD HH:MM}
**Workflow ID**: $WORKFLOW_ID
**Status**: {ALL_PASS | FIXED | BLOCKED}

## Summary

| Check | Result | Details |
|-------|--------|---------|
| BUILD completeness | ✅ | {N} changed .py files, all covered |
| Bazel build | ✅ | `bazel build //...` exit 0 |
| ruff check | ✅ | changed files only |
| ruff format | ✅ | changed files only |
| mypy | ✅ | changed files only |
| Unit tests | ✅ | {N} passed, {M} skipped |

## Changed Files Checked

{list $CHANGED_PY, or "none"}

## Issues Fixed During Validation

{file → fix table, or "none"}
```

---

## Phase 8: OUTPUT — Report Results

### If All Pass:

```markdown
## LMAO Validation Complete ✅

| Check | Status |
|-------|--------|
| BUILD completeness | ✅ |
| Bazel build | ✅ |
| ruff | ✅ |
| ruff format | ✅ |
| mypy | ✅ |
| Unit tests | ✅ ({N} passed) |

Artifact: `$ARTIFACTS_DIR/validation.md`
Next: hardware E2E gate (`lmao-hardware-e2e`).
```

### If Blocked (unfixable):

```markdown
## LMAO Validation Blocked ❌

**Failed check**: {name} — {error}
**Attempts**: {what was tried}
**Required action**: {what a human must do}

Partial results in `$ARTIFACTS_DIR/validation.md`.
```

A blocked validation FAILS this node — do not report success and do not continue.

---

## Success Criteria

- **COMPLETENESS_PASS**: every changed non-exempt `.py` resolves to a Bazel target
- **BUILD_PASS**: `bazel build //...` exits 0
- **LINT_PASS**: `ruff check` + `ruff format --check` clean on changed files
- **TYPES_PASS**: `mypy` clean on changed files
- **TESTS_PASS**: `bazel test //tests:all --test_tag_filters=-requires_hardware` all green
- **ARTIFACT_WRITTEN**: `$ARTIFACTS_DIR/validation.md` documents all results
