---
description: Smart-irrigation validation gate — LMAO Bazel checks (BUILD completeness, build, ruff, mypy, unit tests) plus MicroPython syntax, vendored-copy integrity and control-engine sweep checks. No hardware.
argument-hint: (no arguments - validates current branch changes vs $BASE_BRANCH)
---

# Smart Irrigation — Validate

**Workflow ID**: $WORKFLOW_ID

## ⚠️ Project rules (MANDATORY)

- **Never esptool-probe/flash any device**; no ad-hoc serial access in this gate.
- **NEVER write `$ARTIFACTS_DIR/.gate-head`** — only `irrigation-production-health`
  owns that marker. Writing it here makes downstream hardware gates fast-pass
  without executing (issue #100 regression guard).
- Bazel is canonical; vendored `firmware/lib/urns/**` is lint/BUILD-exempt —
  never "clean it up". Vendored copies must stay byte-identical to
  `cardputer_client/`.

---

## Your Mission

Run the full host-side validation gate on this branch's changes and fix
failures. No hardware. This is the Bazel-native LMAO gate plus
smart-irrigation-specific checks.

---

## Phase -1: LOCATE THE RUN TREE (MANDATORY)

```bash
git worktree list --porcelain | grep -E "^(worktree|branch)"
```

Use this run's worktree (`~/.archon/workspaces/*/worktrees/archon/task-*`);
fall back to the current checkout for `--no-worktree` runs. Every git/bazel
command below runs there:

```bash
WT=/absolute/path/to/run-tree
cd "$WT"
```

---

## Phase 0: FAST-PASS

```bash
cd "$WT"
if [ -f "$ARTIFACTS_DIR/.gate-head" ] && \
   [ "$(cat "$ARTIFACTS_DIR/.gate-head")" = "$(git rev-parse HEAD)" ] && \
   [ -f "$ARTIFACTS_DIR/validation.md" ]; then
  echo "FAST-PASS"
fi
```

If `FAST-PASS` prints, output:

```markdown
## Validation ✅ (fast-pass — no changes since last gate)
```

and STOP. **Do NOT write `.gate-head`** — review/fix commits invalidate it, and
the full gate below becomes mandatory again.

---

## Phase 1: SCOPE — Changed Files

```bash
CHANGED_PY=$( { git diff --name-only "$BASE_BRANCH"...HEAD -- '*.py'; git ls-files --others --exclude-standard -- '*.py'; } | sort -u | grep -v '^bazel-' || true )
CHANGED_FW=$(echo "$CHANGED_PY" | grep '^smart_irrigation/firmware/' || true)
CHANGED_IRR=$(echo "$CHANGED_PY" | grep '^smart_irrigation/' || true)
echo "$CHANGED_PY"
```

Lint/type checks run on `$CHANGED_PY` only. Never reformat files outside the
changed set.

If `$CHANGED_PY` is empty, still run Phases 2, 3, 6 and mark the rest N/A.

---

## Phase 2: BUILD COMPLETENESS (irrigation exemption list)

A new Python file without a Bazel target silently never runs (issue #87). Run:

```bash
EXEMPT_RE='(^|/)__init__\.py$|^proto/.*_pb2(_grpc)?\.py$|^rnode_firmware/|^diagnose_stack\.py$|^tests/test_paths\.py$|^tests/e2e/test_debug_paths\.py$|^tools/archon_webhook_relay\.py$|^smart_irrigation/firmware/lib/urns/'
MISSING=0
for f in $CHANGED_PY; do
  echo "$f" | grep -qE "$EXEMPT_RE" && { echo "EXEMPT:  $f"; continue; }
  pkg_dir=$(dirname "$f")
  while [ "$pkg_dir" != "." ] && [ ! -f "$pkg_dir/BUILD" ]; do pkg_dir=$(dirname "$pkg_dir"); done
  if [ "$pkg_dir" = "." ]; then
    [ -f BUILD ] && label="//:$f" || label=""
  else
    label="//$pkg_dir:${f#$pkg_dir/}"
  fi
  if [ -n "$label" ] && bazel query "$label" >/dev/null 2>&1; then
    echo "COVERED: $f"
  else
    echo "MISSING: $f  ← not referenced by any BUILD target"
    MISSING=1
  fi
done
```

Irrigation exemptions (do not extend without recording the justification in
`validation.md`):

| Pattern | Why exempt |
|---------|------------|
| `smart_irrigation/firmware/lib/urns/**` | Vendored MicroPython µReticulum port; copied to the device, never executed by host Python (mirror of `cardputer_client/lib/urns`) |

MicroPython entry files (`main.py`, `boot.py`, `config.py`, `lora_boards.py`,
`flash.py`) must resolve via `exports_files`/`filegroup` in the package BUILD.
Wire every MISSING file into the appropriate BUILD, then re-run until clean.

**Record**: ✅ Pass / ❌ Fail (fixed)

---

## Phase 3: BAZEL BUILD

```bash
bazel build //...
```

Must exit 0. **Record**: ✅ / ❌ (fixed)

---

## Phase 4: LINT + TYPES (changed files)

```bash
ruff check $CHANGED_PY
ruff format --check $CHANGED_PY
mypy $CHANGED_PY
```

Auto-fix with `ruff check --fix` / `ruff format`, then re-check. Configs:
`ruff.toml`, `mypy.ini`. Never touch vendored files.

**Record**: ✅ / ❌ (fixed)

---

## Phase 5: IRRIGATION-SPECIFIC CHECKS

Only the checks whose inputs exist; mark the rest N/A.

### 5.1 MicroPython syntax (changed firmware files)

```bash
for f in $CHANGED_FW; do
  echo "$f" | grep -qE '^smart_irrigation/firmware/lib/' && continue
  if command -v mpy-cross >/dev/null 2>&1; then
    mpy-cross -o /dev/null "$f" || echo "SYNTAX FAIL: $f"
  else
    python3 -m py_compile "$f" || echo "SYNTAX FAIL: $f"
  fi
done
```

(`py_compile` catches syntax errors only — MicroPython API misuse is covered by
mock tests. Note in the artifact which tool was used.)

### 5.2 Vendored copy integrity

For every `smart_irrigation/firmware/lib/urns/**` and
`smart_irrigation/firmware/proto/lma_encoder.py` present, diff against the
`cardputer_client/` source:

```bash
for f in $(find smart_irrigation/firmware/lib/urns -name '*.py' 2>/dev/null); do
  src="cardputer_client/${f#smart_irrigation/firmware/}"
  if [ -f "$src" ]; then
    diff -q "$src" "$f" >/dev/null || echo "DRIFT: $f vs $src"
  else
    echo "NO_SOURCE: $f (no cardputer_client counterpart)"
  fi
done
```

Drift is a FAIL unless the plan explicitly documents an intentional adaptation
(record the justification). MicroPython-only adaptations (pin aliases) must be
limited to `config.py`/`lora_boards.py` — never inside `urns/`.

### 5.3 Control-engine evidence

If the evaluation chose fuzzy/hybrid and a rule matrix or membership module
changed, verify host tests cover:
- matrix shape (full 60 rules or the documented compressed set),
- monotonicity sweep (drier soil ⇒ ≥ pump time modulo overrides),
- every safety override wins over any control output,
- fail-off behavior on sensor error/NaN.

Run the relevant `bazel test //smart_irrigation/...` targets. If no control
engine exists yet (early phases), N/A.

### 5.4 Protocol round-trip

If `lma_encoder`/proto or sensor-id code changed: the SensorReport /
CommandRequest / CommandAck round-trip tests must pass against the existing
`tests/test_lma_encoder.py` behavior. Wire changes without a round-trip test
are a FAIL.

### 5.5 Reference-tool smoke (evaluation assets)

If `smart_irrigation/scripts/*` changed and a sim/benchmark entrypoint exists,
run it with a tiny bounded sweep (e.g. 10-point grid) and check it exits 0.

**Record** each sub-check: ✅ / ❌ / N/A.

---

## Phase 6: UNIT TESTS

```bash
bazel test //tests:all --test_tag_filters=-requires_hardware --test_output=errors
```

Also run irrigation targets explicitly when present:

```bash
if bazel query 'tests(//smart_irrigation/...)' 2>/dev/null | grep -q .; then
  bazel test $(bazel query 'tests(//smart_irrigation/...)' 2>/dev/null | tr '\n' ' ') --test_output=errors
fi
```

(If `//smart_irrigation/...` has no test targets yet, skip with N/A.)

Hardware E2E targets are excluded here — `irrigation-hardware-e2e` runs next.

**Record**: ✅ Pass ({N}) / ❌ Fail (fixed)

---

## Phase 7: ARTIFACT

Write `$ARTIFACTS_DIR/validation.md`:

```markdown
# Smart Irrigation Validation Results

**Generated**: {YYYY-MM-DD HH:MM}
**Workflow ID**: $WORKFLOW_ID
**Status**: {ALL_PASS | FIXED | BLOCKED}

## Summary

| Check | Result | Details |
|-------|--------|---------|
| BUILD completeness | ✅ | {N} changed .py, all covered ({M} vendored-exempt) |
| Bazel build | ✅ | exit 0 |
| ruff check / format | ✅ | changed files |
| mypy | ✅ | changed files |
| MicroPython syntax | ✅ / N/A | {mpy-cross | py_compile} |
| Vendored integrity | ✅ / N/A | {N} files identical |
| Control-engine evidence | ✅ / N/A | {tests} |
| Protocol round-trip | ✅ / N/A | {tests} |
| Reference-tool smoke | ✅ / N/A | {script} |
| Unit tests | ✅ | {N} passed, {M} skipped |

## Changed Files Checked
{list}

## Justified Exemptions / Intentional Drift
{none | file → reason}

## Issues Fixed During Validation
{file → fix}
```

---

## Phase 8: OUTPUT

### All pass

```markdown
## Validation Complete ✅

| Check | Status |
|-------|--------|
| BUILD completeness | ✅ |
| Bazel build | ✅ |
| ruff + mypy | ✅ |
| MicroPython syntax | ✅ / N/A |
| Vendored integrity | ✅ |
| Control/protocol evidence | ✅ / N/A |
| Unit tests | ✅ ({N}) |

Artifact: `$ARTIFACTS_DIR/validation.md`
Next: hardware E2E gate (`irrigation-hardware-e2e`).
```

### Blocked

Report the failed check, what was attempted, and what a human must do; FAIL the
node. Partial results go in the artifact.

## Success Criteria

- **COMPLETENESS_PASS**: every changed non-exempt `.py` resolves to a target
- **BUILD_PASS**: `bazel build //...` exit 0
- **HOST_CHECKS_PASS**: ruff/mypy/unit tests green on the changed set
- **IRRIGATION_CHECKS_PASS**: syntax, vendored integrity, control/protocol evidence green or N/A
- **NO_MARKER**: `.gate-head` untouched
- **ARTIFACT_WRITTEN**: `validation.md` documents every check
