---
description: Execute the smart-irrigation plan task-by-task with Bazel/MicroPython checks after every change. Firmware safety and BUILD-completeness rules enforced.
argument-hint: (no arguments - reads from workflow artifacts)
---

# Smart Irrigation — Implement Tasks

**Workflow ID**: $WORKFLOW_ID

## ⚠️ Project rules (MANDATORY)

Read and obey `smart_irrigation/AGENTS.md` and `/home/pondycrane/LMAO/AGENTS.md`:

- **Never esptool-probe/flash any device**, never open ad-hoc serial sessions.
  No hardware access in this phase at all — hardware E2E runs in its own gate.
- Leave the production Cardputer running (this phase must not touch it).
- MicroPython only on the Atom Lite; Q8.8 fixed-point control engine (≤ 30 KB
  RAM target); I2C 100 kHz; deep sleep + watchdog + heap recovery inheritance.
- Pump safety overrides are hard-coded, fail-off, and unit-tested.
- Every new `*.py` gets its BUILD wiring in the same task (missing target =
  silently never tested; issue #87 regression class).
- Vendored `firmware/lib/urns/**` copies stay byte-identical to
  `cardputer_client/lib/urns/**` — never edit or lint them.

---

## Your Mission

Execute the plan's tasks in order, validating after every change. Implement
exactly what the plan says — deviations must be documented, and the safety
rules above always win over the plan.

**Output artifact**: `$ARTIFACTS_DIR/implementation.md` plus committed-free
working-tree changes (the finalize step commits/pushes).

---

## Phase -1: LOCATE THE RUN TREE (MANDATORY)

```bash
git worktree list --porcelain | grep -E "^(worktree|branch)"
```

Use this run's worktree (typically `~/.archon/workspaces/*/worktrees/archon/task-*`);
fall back to the current checkout for `--no-worktree` runs.

```bash
WT=/absolute/path/to/run-tree
cd "$WT"
```

All edits happen inside `"$WT"`.

---

## Phase 1: LOAD CONTEXT

```bash
cat $ARTIFACTS_DIR/plan-context.md
cat $ARTIFACTS_DIR/plan-confirmation.md
cat $ARTIFACTS_DIR/plan.md
cat $ARTIFACTS_DIR/evaluation.md
```

Extract:
- The CREATE/UPDATE file list and task order
- The **evaluation verdict** and which parts of it this feature implements
- Validation commands per task (`VALIDATE:` lines)
- Scope limits (`NOT Building`) — do not implement beyond them
- Safety overrides and hardware assumptions that must be preserved

If `plan.md` is missing, STOP with an error.

**CHECKPOINT**: plan + evaluation verdict loaded; task list in order.

---

## Phase 2: IMPLEMENT (per task)

For each task in plan order:

1. **Mirror the pattern** named in the task (`MIRROR:` file:lines). Match naming,
   import guards, logging, error handling, and comment style exactly.
2. **MicroPython portability**: device logic must not import host-only modules
   at top level; guard `machine`, `micropython`, `esp32`, `uasyncio` imports
   the way the existing client does. Host-testable modules must import cleanly
   on CPython (the `py_test` will run there).
3. **Fixed-point discipline**: if the evaluation selected a Q8.8 engine, no
   float arithmetic in the hot path; document the scale conversions at the
   boundary (sensor read → Q8.8 → actuator ms). If the evaluation selected a
   model-based algorithm, keep its fixed-point contract.
4. **Safety overrides first**: implement hard overrides before the control
   output is applied; every override must fail off on exception. Never let a
   fuzzy/model output bypass an override.
5. **Protocol compatibility**: reuse `lma_encoder` functions/field numbers;
   add a host round-trip test for any wire change. New `sensor_id`s must match
   blueprint §3.1 exactly.
6. **BUILD wiring in the same task**:
   - host-testable module → `py_library` (+ `deps`, `imports = ["."]` where
     siblings use it)
   - new test → `py_test` mirroring `tests/BUILD` conventions (`conftest.py` in
     `srcs` when siblings do), tag `requires_hardware` only for real hardware
   - MicroPython entry files → `exports_files`/`filegroup`
   - vendored `firmware/lib/urns/**` → covered by the glob `filegroup` only
7. **Validate after the change** (task's `VALIDATE` command, at minimum):
   ```bash
   bazel build //...            # or the narrower affected targets while iterating
   bazel test //tests:<affected> --test_output=errors
   ruff check <changed> && ruff format --check <changed> && mypy <changed>
   ```
   Fix before moving on. Do not leave a red tree for the next task.
8. **Update docs in-task**: `docs/pin-mapping.md` for confirmed pins,
   `docs/development-plan.md` checkboxes for completed phase items,
   `docs/algorithm-evaluation.md` only if the evaluation is being committed
   (never silently change its verdict — note corrections explicitly).

### Hardware-unverifiable work (STM32 DTU, sensor drivers without a device)

Implement to spec, compile/build what can be built (PlatformIO for the DTU,
host tests with mocks for drivers), and record in `implementation.md` exactly
what could **not** be verified without hardware. Never fabricate a hardware
result.

---

## Phase 3: SELF-CHECK

- [ ] Every task in the plan is done or explicitly marked skipped with reason
- [ ] `bazel build //...` green
- [ ] All affected unit tests green; new logic has tests
- [ ] Every safety override implemented + tested
- [ ] No float in the control hot path (when Q8.8 is selected)
- [ ] Vendored copies byte-identical to source (`diff -rq` where feasible)
- [ ] No hardware was touched; production Cardputer untouched
- [ ] BUILD completeness: `git status --porcelain` new `.py` files all resolve
      to a target
- [ ] Artifacts not committed (they live in `$ARTIFACTS_DIR`)

---

## Phase 4: WRITE `$ARTIFACTS_DIR/implementation.md`

```markdown
# Implementation Report

**Workflow ID**: $WORKFLOW_ID
**Status**: {COMPLETE | PARTIAL | BLOCKED}

## Tasks

| # | Task | Status | Validation | Notes |
|---|------|--------|------------|-------|
| 1 | {create/update file} | ✅/❌/⏭ | {command → result} | |

## Deviations from Plan

| Plan said | Actually did | Why |
|-----------|--------------|-----|

## Hardware-Unverifiable Items

| Item | Why unverifiable | How to verify when hardware attached |
|------|------------------|--------------------------------------|

## Safety Overrides Implemented

| Override | File | Test |
|----------|------|------|

## Build/Test Evidence

{paste the final bazel/ruff/mypy command outputs — concise}
```

---

## Phase 5: OUTPUT

```markdown
## Implementation {✅ COMPLETE | ⚠️ PARTIAL | ❌ BLOCKED}

**Tasks**: {done}/{total} · **Files**: {C} created, {U} updated
**Build**: ✅ `bazel build //...` · **Tests**: ✅ {N} passed
**Artifact**: `$ARTIFACTS_DIR/implementation.md`
Next: validation gate (`irrigation-validate`).
```

## Success Criteria

- **PLAN_EXECUTED**: every task done or explicitly deferred with a reason
- **BUILD_GREEN**: `bazel build //...` exits 0 after every task
- **TESTS_ADDED**: new logic covered by host tests (or recorded as
  hardware-unverifiable)
- **BUILD_WIRED**: every new `.py` resolves to a Bazel target
- **SAFETY_PRESERVED**: overrides implemented, fail-off, tested
- **NO_HARDWARE_ACCESS**: no device was probed, flashed, or opened
- **ARTIFACT_WRITTEN**: `$ARTIFACTS_DIR/implementation.md` complete
