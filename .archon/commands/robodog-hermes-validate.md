---
description: RoboDog hermes validation gate — BUILD completeness, bazel build, ruff, mypy, unit tests + optional hermes firmware build (build_robodog_hermes) + capture/corpus sanity. Bazel-native, no hardware.
argument-hint: (no arguments - validates current branch changes vs $BASE_BRANCH)
---

# RoboDog Hermes Validate

**Workflow ID**: $WORKFLOW_ID

## ⚠️ RoboDog Hermes Project Rules (MANDATORY — from AGENTS.md + README "Voice mode")

**Hardware safety — violating these can brick physical devices:**

1. **NEVER run esptool on the Cardputer** (`0x303A:0x8120`) — no flashing, no
   `chip_id`, no probing. The Cardputer is flashed ONLY via
   `bazel run //cardputer_client:flash`.
2. **NEVER flash the RNode** (`/dev/ttyUSB*`) — the only supported method is the
   web flasher at https://flasher.rnode.network/.
3. The hermes board flash happens in `robodog-hermes-e2e`, NOT here. This node
   is host-only: `bazel build //...` does NOT compile the firmware (all
   `//robodog_client:*` firmware targets are `tags = ["manual"]`). When hermes
   firmware sources changed (`robodog_client/main/**`, `robodog_client/voice/**`,
   the patches), this node runs `bazel run //robodog_client:build_robodog_hermes`
   so build errors surface BEFORE the board is touched. NEVER build/flash the
   calendar firmware target here.
4. Leave the production Cardputer running — nothing in this node touches it.

**Gate-chain safety — violating these can silently skip mandatory tests:**

- **NEVER write `$ARTIFACTS_DIR/.gate-head`** — only `robodog-production-health`
  owns that marker. Writing it from any other gate node causes downstream
  hardware gates to fast-pass without executing (issue #100 guard).

## Your Mission

Run the hermes validation gate on the current branch's changes and fix any
failures. Bazel-native replacement for `archon-validate` plus the hermes host
units. Focused step: run checks, fix issues, repeat until green.

## Phase -1: LOCATE THE RUN TREE (MANDATORY)

Run all phases with strict shell semantics and a validated base ref:

```bash
set -euo pipefail
[ -n "${BASE_BRANCH:-}" ] || { echo "BASE_BRANCH is unset/empty — abort" >&2; exit 2; }
git rev-parse --verify "$BASE_BRANCH" >/dev/null 2>&1 || { echo "BASE_BRANCH '$BASE_BRANCH' is not a ref" >&2; exit 2; }
```

```bash
git worktree list --porcelain | grep -E "^(worktree|branch)"
```
Use the worktree for this run's branch if it exists, else the current checkout.
Set it once (`cd "$WT"`) and stick to it — never mix trees (issue-#91 guard).

## Phase 0: FAST-PASS — Skip If Nothing Changed

```bash
cd "$WT"
if [ -f "$ARTIFACTS_DIR/.gate-head" ] && [ "$(cat "$ARTIFACTS_DIR/.gate-head")" = "$(git rev-parse HEAD)" ] \
   && [ -f "$ARTIFACTS_DIR/validation.md" ]; then
  echo "FAST-PASS"
fi
```
If `FAST-PASS` printed: output `## RoboDog Hermes Validation ✅ (fast-pass — no changes since last gate)` and STOP. Do NOT write `.gate-head`.

## Phase 1: SCOPE — Changed Python + Firmware Files

```bash
cd "$WT"
CHANGED_PY=$( { git diff --name-only "$BASE_BRANCH"...HEAD -- '*.py'; git ls-files --others --exclude-standard -- '*.py'; } | sort -u | grep -v '^bazel-' || true )
CHANGED_C=$( { git diff --name-only "$BASE_BRANCH"...HEAD -- '*.c' '*.h'; } | sort -u || true )
echo "$CHANGED_PY"; echo "$CHANGED_C"
```
Lint/type run on `$CHANGED_PY` **only** (master carries pre-existing lint debt
in vendored/standalone files — never "fix" files outside the changed set).

## Phase 2: BUILD COMPLETENESS — Every Changed .py Belongs To A Target

A new Python file not referenced by any Bazel target is silently never built or
tested (issue #87). Exemptions (same list as `robodog-validate`, do not extend
without documenting):

```bash
EXEMPT_RE='(^|/)__init__\.py$|^proto/.*_pb2(_grpc)?\.py$|^rnode_firmware/|^diagnose_stack\.py$|^tools/archon_webhook_relay\.py$|^robodog_client/capture\.py$'
MISSING=0
for f in $CHANGED_PY; do
  echo "$f" | grep -qE "$EXEMPT_RE" && { echo "EXEMPT:    $f"; continue; }
  pkg_dir=$(dirname "$f")
  while [ "$pkg_dir" != "." ] && [ ! -f "$pkg_dir/BUILD" ]; do pkg_dir=$(dirname "$pkg_dir"); done
  if [ "$pkg_dir" = "." ]; then
    [ -f BUILD ] && label="//:$f" || label=""
  else
    # The voice tools live under robodog_client/voice/ but are wired from
    # robodog_client/BUILD (sh_binary data) — match by reverse-dep so a file
    # referenced from any target in the package counts as covered.
    label="//$pkg_dir:${f#$pkg_dir/}"
  fi
  if [ -n "$label" ] && bazel query "rdeps('//$pkg_dir/...', '$label', 1)" >/dev/null 2>&1; then
    echo "COVERED:   $f"
  else
    echo "MISSING:   $f  ← not referenced by any BUILD target or exempt list"
    MISSING=1
  fi
done
```

For every MISSING file: wire it into the appropriate BUILD target (e.g. extend
`//robodog_client:hermes_voice_e2e` data / `:voice_backend`), then re-run until
clean. (Both `robodog-validate` and `robodog-hermes-validate` share this
rdeps matching, including `voice/*.py`.)

The state-screen feature adds `//robodog_client:statescreen_check` (a
pure-stdlib `py_library` mirroring the `e2e_readout` pattern, consumed by
`//tests:test_statescreen_check` and carried as data by `:hermes_voice_e2e`)
and extends `:voice_backend` with `voice/hermes-platform/xiaozhi/*` (the
overridden xiaozhi adapter now ships in the deploy filegroup).

**Record**: ✅ Pass / ❌ Fail (fixed)

## Phase 3: BAZEL BUILD

```bash
cd "$WT" && bazel build //...
```
Must exit 0 (also regenerates protobuf stubs). Fix BUILD dependency errors.

## Phase 4: LINT

```bash
cd "$WT" && ruff check $CHANGED_PY && ruff format --check $CHANGED_PY
```
`ruff check --fix` then manual for the rest. Config `ruff.toml`.

## Phase 5: TYPE CHECK

```bash
cd "$WT" && mypy $CHANGED_PY
```
Config `mypy.ini`. Never `mypy .`.

## Phase 6: UNIT TESTS

```bash
cd "$WT" && bazel test //tests:all --test_tag_filters=-requires_hardware --test_output=errors
```
All must pass. Hardware E2E targets (`requires_hardware`) run in the hermes
hardware gates next.

## Phase 7: HERMES HOST TOOLING

### 7.1 Firmware compile check (when C/voice sources changed)

`bazel build //...` skips the manual-tagged firmware targets; when `$CHANGED_C`
is non-empty (under `robodog_client/`) or any `robodog_client/voice/**` / the
hermes patches changed, run the real XiaoZhi build:
```bash
cd "$WT" && bazel run //robodog_client:build_robodog_hermes
```
Needs Docker + the pinned upstream fetch; takes minutes. If Docker is
unavailable, record `FIRMWARE_COMPILE=UNVERIFIABLE (docker unavailable)` —
`robodog-hermes-e2e` does the compile+flash next.

### 7.2 Voice toolchain sanity (always)

```bash
python3 robodog_client/capture.py --help                                  # capture import parse
~/xiaozhi/.venv/bin/python -c "import serial, PIL, fastapi; print('venv ok')"  # ops venv deps present
bazel run //robodog_client:make_corpus -- --verify   # pinned corpus intact (sha256)
```
`make_corpus --verify` failing means the committed fixtures drifted — regenerate
via `bazel run //robodog_client:make_corpus` and include in the change.

**Record**: ✅ selftests + (firmware compile / UNVERIFIABLE) + corpus verify

## Phase 8: ARTIFACT — Write Validation Results

Write `$ARTIFACTS_DIR/validation.md`:

```markdown
# RoboDog Hermes Validation Results
**Generated**: {YYYY-MM-DD HH:MM}   **Workflow ID**: $WORKFLOW_ID
**Status**: {ALL_PASS | FIXED | BLOCKED}

| Check | Result | Details |
|-------|--------|---------|
| BUILD completeness | ✅ | {N} changed .py files, all covered |
| Bazel build | ✅ | `bazel build //...` exit 0 |
| ruff check / format | ✅ | changed files only |
| mypy | ✅ | changed files only |
| Unit tests | ✅ | {N} passed, {M} skipped |
| hermes firmware compile | ✅ / UNVERIFIABLE | build_robodog_hermes |
| corpus verify | ✅ | {N} fixtures match sha256 |
```
Include the files→fix table (or "none") and any deviations, then STOP.
