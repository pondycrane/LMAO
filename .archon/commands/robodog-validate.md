---
description: RoboDog validation gate — BUILD completeness, bazel build, ruff, mypy, unit tests + lma_proto host selftest + optional ESP-IDF firmware build. Bazel-native, no hardware.
argument-hint: (no arguments - validates current branch changes vs $BASE_BRANCH)
---

# RoboDog Validate

**Workflow ID**: $WORKFLOW_ID

## ⚠️ RoboDog Project Rules (MANDATORY — from AGENTS.md + robodog_client/README.md)

**Hardware safety — violating these can brick physical devices:**

1. **NEVER run esptool on the Cardputer** (`0x303A:0x8120`) — no flashing, no
   `chip_id`, no probing. esptool kills the USB-Serial-JTAG interface; recovery
   needs a physical replug. The Cardputer is flashed ONLY via
   `bazel run //cardputer_client:flash`.
2. **NEVER flash the RNode via esptool** — the only supported method is the web
   flasher at https://flasher.rnode.network/. An interrupted esptool flash bricks
   the device.
3. **The robodog board flash happens in `robodog-hardware-e2e`, NOT here.**
   This node is host-only: `bazel build //...` does NOT compile the firmware
   (`//robodog_client:*` targets are `tags = ["manual"]`) — when
   `robodog_client/main/**` or `components/**` changed, this node runs the
   `bazel run //robodog_client:build_firmware` compile check (ESP-IDF Docker)
   so C errors surface BEFORE the board is touched.
4. Leave the production Cardputer running — nothing in this node touches it.

**Build rules:**

- **Bazel is the canonical build** (v7.4.1 via bazelisk). Every new `*.py` file
  MUST belong to a BUILD target — a test file without a `py_test` target
  silently never runs (issue #87).
- Lint: `ruff check` / `ruff format --check` (config `ruff.toml`). Type-check:
  `mypy` (config `mypy.ini`). Run them on changed files only — master carries
  pre-existing debt in vendored/standalone files.
- Vendored code is lint-exempt: `robodog_client/components/u8g2/**` (csrc fetched
  by `u8g2_prepare.sh`), `robodog_client/components/u8g2_st7305/**`,
  `cardputer_client/lib/urns/**`, `rnode_firmware/esptool.py`.
- Unit gate: `bazel test //tests:all --test_tag_filters=-requires_hardware`
  must pass. Hardware E2E is NOT part of this command —
  `robodog-hardware-e2e` runs it next.

**Gate-chain safety — violating these can silently skip mandatory tests:**

- **NEVER write `$ARTIFACTS_DIR/.gate-head`** — only `robodog-production-health`
  owns that marker. Writing it from any other gate node causes downstream
  hardware E2E to fast-pass without executing (issue #100 guard).

---

## Your Mission

Run the RoboDog validation gate on the current branch's changes and fix any
failures. Bazel-native replacement for `archon-validate` plus the robodog host
units. Focused step: run checks, fix issues, repeat until green.

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

NEVER mix trees: a `git rev-parse HEAD` in the wrong checkout silently corrupts
the fast-pass marker logic (observed in the issue-#91 live test).

---

## Phase 0: FAST-PASS — Skip If Nothing Changed

```bash
cd "$WT"
if [ -f "$ARTIFACTS_DIR/.gate-head" ] && [ "$(cat "$ARTIFACTS_DIR/.gate-head")" = "$(git rev-parse HEAD)" ] \
   && [ -f "$ARTIFACTS_DIR/validation.md" ]; then
  echo "FAST-PASS"
fi
```

If `FAST-PASS` printed: HEAD is unchanged since the last full gate chain
completed, so re-running would re-test identical code. Output exactly:

```markdown
## RoboDog Validation ✅ (fast-pass — no changes since last gate)
```

and STOP.

**🚫 DO NOT WRITE `.gate-head`**: it is owned EXCLUSIVELY by
`robodog-production-health`. Premature writes cause downstream hardware gates to
fast-pass without executing (issue #100 regression guard).

---

## Phase 1: SCOPE — Changed Python + Firmware Files

Run inside `"$WT"`:

```bash
CHANGED_PY=$( { git diff --name-only "$BASE_BRANCH"...HEAD -- '*.py'; git ls-files --others --exclude-standard -- '*.py'; } | sort -u | grep -v '^bazel-' || true )
echo "$CHANGED_PY"
CHANGED_C=$( { git diff --name-only "$BASE_BRANCH"...HEAD -- '*.c' '*.h'; } | sort -u || true )
echo "$CHANGED_C"
```

All lint/type checks run on `$CHANGED_PY` **only**. Never reformat or "fix"
files outside the changed set — master carries pre-existing lint debt in
vendored/standalone files.

If `$CHANGED_PY` is empty, Phases 4–5 are skipped (note it in the artifact);
Phases 2, 3, 6 and 7 still run.

---

## Phase 2: BUILD COMPLETENESS — Every Changed .py Belongs To A Target

A new Python file not referenced by any Bazel target is silently never built or
tested — the exact regression class from issue #87. Run verbatim:

```bash
EXEMPT_RE='(^|/)__init__\.py$|^proto/.*_pb2(_grpc)?\.py$|^rnode_firmware/|^diagnose_stack\.py$|^tests/test_paths\.py$|^tests/e2e/test_debug_paths\.py$|^tools/archon_webhook_relay\.py$|^robodog_client/capture\.py$'
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
  if [ -n "$label" ] && bazel query "rdeps('//$pkg_dir/...', '$label', 1)" >/dev/null 2>&1; then
    echo "COVERED:   $f"
  else
    echo "MISSING:   $f  ← not referenced by any BUILD target"
    MISSING=1
  fi
done
```

The exemption list (do not extend without writing the justification into
`$ARTIFACTS_DIR/validation.md`):

| Pattern | Why exempt |
|---------|------------|
| `**/__init__.py` | Package markers — optional in Bazel `py_library` |
| `proto/*_pb2.py`, `proto/*_pb2_grpc.py` | Bazel generates these from `.proto` |
| `rnode_firmware/**` | Vendored firmware tooling, never built |
| `diagnose_stack.py` | Standalone diagnostic script run with plain `python3` |
| `tests/test_paths.py`, `tests/e2e/test_debug_paths.py` | Debug utilities, not tests |
| `tools/archon_webhook_relay.py` | Standalone script run via systemd |
| `robodog_client/capture.py` | Standalone host capture/vision tool (run with plain `python3`; not part of the ESP-IDF build) |

For every MISSING file, wire it into the appropriate `BUILD` file, then re-run
this phase until nothing is MISSING.

**Record result**: ✅ Pass / ❌ Fail (fixed)

---

## Phase 3: BAZEL BUILD

```bash
bazel build //...
```

Must exit 0 (also regenerates protobuf stubs). Fix BUILD dependency errors
before continuing.

**Record result**: ✅ Pass / ❌ Fail (fixed)

---

## Phase 4: LINT

```bash
ruff check $CHANGED_PY
ruff format --check $CHANGED_PY
```

If `ruff check` fails: try `ruff check --fix $CHANGED_PY`, then manually fix the
rest. If format fails: `ruff format $CHANGED_PY`, re-check. Config: `ruff.toml`
(line-length 100, target py310). Respect per-file-ignores.

**Record result**: ✅ Pass / ❌ Fail (fixed)

---

## Phase 5: TYPE CHECK

```bash
mypy $CHANGED_PY
```

Config: `mypy.ini` (`ignore_missing_imports = True`). Do NOT run `mypy .`.

**Record result**: ✅ Pass / ❌ Fail (fixed)

---

## Phase 6: UNIT TESTS

```bash
bazel test //tests:all --test_tag_filters=-requires_hardware --test_output=errors
```

All must pass. Hardware E2E targets (`requires_hardware`) run in
`robodog-hardware-e2e` next. The K8s test auto-skips when no cluster is reachable.

If a test fails: determine whether the implementation or the test is wrong, fix
the root cause, re-run.

**Record result**: ✅ Pass ({N} tests) / ❌ Fail (fixed)

---

## Phase 7: ROBODOG HOST UNITS + FIRMWARE COMPILE

Run these robodog-specific checks inside `"$WT"`:

### 7.1 Wire-layer host selftest (always)

The dependency-free protobuf codec is host-testable — prove the wire layer:
```bash
cc -std=c11 -Wall -Wextra robodog_client/main/lma_proto_selftest.c \
   robodog_client/main/lma_proto.c -o /tmp/lma_selftest && /tmp/lma_selftest
```

### 7.1b Render/datetime host selftest (always)

The panel row-meta qualifiers (today / tomorrow / MM/DD / YYYY/MM/DD), the
EDT civil-day underflow math, and the measured clamp-length silhouette are
host-testable pure units in `rd_datetime.c` + `rd_clip.c`:
```bash
cc -std=c11 -Wall -Wextra robodog_client/main/rd_datetime_selftest.c \
   robodog_client/main/rd_datetime.c robodog_client/main/rd_clip.c \
   -o /tmp/rd_dt_selftest && /tmp/rd_dt_selftest
```

### 7.2 Firmware compile check (when C sources changed)

`bazel build //...` skips the manual-tagged firmware targets, so a C error would
otherwise wait until the hardware gate. When `$CHANGED_C` is non-empty (under
`robodog_client/`), run the real ESP-IDF build:
```bash
bazel run //robodog_client:build_firmware
```
This needs Docker (`espressif/idf:v5.3.1`) and takes minutes. If Docker is
unavailable, record `FIRMWARE_COMPILE=UNVERIFIABLE (docker unavailable)` in the
artifact — `robodog-hardware-e2e` will do the compile+flash next.

### 7.3 Capture tool sanity

```bash
python3 robodog_client/capture.py --help   # import parse errors surface here
```

**Record result**: ✅ selftest + (firmware compile / UNVERIFIABLE) + capture sanity

---

## Phase 8: ARTIFACT — Write Validation Results

Write to `$ARTIFACTS_DIR/validation.md`:

```markdown
# RoboDog Validation Results

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
| lma_proto host selftest | ✅ | `lma_proto_selftest` exit 0 |
| rd_datetime/clip host selftest | ✅ | `rd_dt_selftest` exit 0 |
| Firmware compile (if C changed) | ✅/UNVERIFIABLE | ESP-IDF Docker build |
| Capture tool sanity | ✅ | `capture.py --help` parses |

## Changed Files Checked
{list $CHANGED_PY / $CHANGED_C, or "none"}

## Issues Fixed During Validation
{file → fix table, or "none"}
```

---

## Phase 9: OUTPUT — Report Results

### If All Pass:

```markdown
## RoboDog Validation Complete ✅

| Check | Status |
|-------|--------|
| BUILD completeness | ✅ |
| Bazel build | ✅ |
| ruff / ruff format | ✅ |
| mypy | ✅ |
| Unit tests | ✅ ({N} passed) |
| lma_proto selftest | ✅ |
| rd_datetime/clip selftest | ✅ | `rd_datetime_selftest` exit 0 |
| Firmware compile | ✅ / UNVERIFIABLE |

Artifact: `$ARTIFACTS_DIR/validation.md`
Next: hardware E2E gate — screen capture + vision readback (`robodog-hardware-e2e`).
```

### If Blocked (unfixable):

```markdown
## RoboDog Validation Blocked ❌

**Failed check**: {name} — {error}
**Attempts**: {what was tried}
**Required action**: {what a human must do}
```

A blocked validation FAILS this node — do not continue.

---

## Success Criteria

- **COMPLETENESS_PASS**: every changed non-exempt `.py` resolves to a Bazel target
- **BUILD_PASS**: `bazel build //...` exits 0
- **LINT_PASS**: `ruff check` + `ruff format --check` clean on changed files
- **TYPES_PASS**: `mypy` clean on changed files
- **TESTS_PASS**: `bazel test //tests:all --test_tag_filters=-requires_hardware` green
- **WIRE_PASS**: `lma_proto_selftest` exits 0
- **FW_PASS**: firmware compiles via `bazel run //robodog_client:build_firmware`
  when C changed (or honestly marked UNVERIFIABLE)
- **ARTIFACT_WRITTEN**: `$ARTIFACTS_DIR/validation.md` documents all results
