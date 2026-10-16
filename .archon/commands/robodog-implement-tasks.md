---
description: Execute RoboDog plan tasks with build checking after each change — Bazel host gate + ESP-IDF firmware build, panel-capture verification for display work, robodog hardware safety rules.
argument-hint: (no arguments - reads from workflow artifacts)
---

# RoboDog Implement Tasks

**Workflow ID**: $WORKFLOW_ID

---

## ⚠️ RoboDog Project Rules (MANDATORY — from AGENTS.md + robodog_client/README.md)

**Hardware safety — violating these can brick physical devices:**

1. **The robodog board is the Waveshare ESP32-S3 RLCD-4.2 (ST7305)** — VID
   `0x303A` PID `0x1001`. It is flashed ONLY via
   `bazel run //robodog_client:flash_firmware -- --port <port>` — host esptool
   (`robodog_client/flash.sh`) because Docker ttyACM RTS passthrough breaks the
   button-free reset. This is the ONE esptool-on-the-robodog-board path that is
   allowed; it re-reads `build/flash_args` so do not hand-esptool.
2. **NEVER run esptool on the Cardputer** (`0x303A:0x8120`) — no flashing, no
   `chip_id`, no probing. It kills the USB-Serial-JTAG interface; recovery needs
   a physical replug. The Cardputer is flashed ONLY via
   `bazel run //cardputer_client:flash`.
3. **NEVER flash the RNode via esptool** — web flasher only.
4. After a robodog flash the NEW firmware owns the panel (it is the dev/kiosk
   display — no production client to restore). A display change is done only
   when its panel-capture shows it.

**Verification is pixel-driven:**
- Any change to `robodog_client/main/**` (robodog.c, lma_proto, config.h,
  fonts) or the picture pipeline must be validated from the panel pixels, never
  a re-render: build → flash → capture →
  `python3 robodog_client/capture.py --port <port> --reset --n 3 --timeout 120
  --describe --out $ARTIFACTS_DIR/robodog-shots` → read the Qwen2.5-VL
  transcription (`frameN_analysis.txt`) and the PNGs, not the model's guess.
  The gate re-runs this anyway — your goal here is to catch obvious render
  breaks while the context is fresh.
- Host-testable units (`lma_proto_selftest.c`, `tests/test_calendar_pics.py`)
  must stay green; they are where wire/picture logic is proven fast.

**Build rules:**
- **Bazel is the canonical build** (v7.4.1 via bazelisk). Every new `*.py`
  MUST belong to a BUILD target — a test without a `py_test` silently never
  runs (issue #87). New tests mirror neighbors (`conftest.py` in `srcs` +
  `imports = ["."]`).
- Lint/type on changed files only: `ruff check`, `ruff format --check`, `mypy`.
  Vendored u8g2/`:components/**` and standalone `robodog_client/capture.py` are
  lint-exempt — do not "clean them up".
- Firmware: `bazel run //robodog_client:build_firmware` (ESP-IDF Docker, u8g2
  fetched by `u8g2_prepare.sh`). Never copy upstream sources into
  `components/`.

---

## Your Mission

Execute each task from the plan, validating after every change.

**Core Philosophy**:
- Type-check / build-check after EVERY file change; fix immediately
- Document any deviations from the plan
- This step assumes setup is complete - branch exists, plan confirmed

---

## Phase 1: LOAD - Read Context

### 1.1 Load Plan Context

```bash
cat $ARTIFACTS_DIR/plan-context.md
```

Extract: files to change (CREATE/UPDATE), validation commands, patterns to mirror.

### 1.2 Load Plan Confirmation

```bash
cat $ARTIFACTS_DIR/plan-confirmation.md
```

Check the status is CONFIRMED or PROCEED WITH CAUTION; note warnings.

### 1.3 Load Original Plan

The plan source path is in `plan-context.md`. Read the full plan for detailed
task instructions:

```bash
cat {plan-source-path}
```

### 1.4 Build System

```bash
bazel build //...                       # host gate (protobuf stubs included)
ruff check <changed-files>
mypy <changed-files>
bazel test //tests:all --test_tag_filters=-requires_hardware --test_output=errors
# firmware (when robodog_client/main/ or components/ changed):
bazel run //robodog_client:build_firmware
```

**PHASE_1_CHECKPOINT:**
- [ ] Plan context loaded
- [ ] Confirmation status verified
- [ ] Original plan loaded
- [ ] Build system understood (bazel + ESP-IDF Docker)

---

## Phase 2: EXECUTE - Implement Each Task

**For each task in the plan's "Tasks" or "Step-by-Step Tasks" section:**

### 2.1 Read Task Context

1. Read the MIRROR file referenced in the task
2. Understand the pattern to follow
3. Note GOTCHA warnings and IMPORTS needed

### 2.2 Implement the Task

- **CREATE**: write new file following the pattern
- **UPDATE**: modify existing file as specified
- Follow patterns exactly - match style, naming, structure

### 2.3 Build-Check Immediately

After EVERY file change:

```bash
bazel build //... && ruff check <changed-files>
```

If `robodog_client/main/**` or `components/**` changed, also run the firmware
build (`bazel run //robodog_client:build_firmware`) — this is the real ESP-IDF
compile check and it must pass before you flash.

**Do NOT accumulate errors** - fix each before moving on.

**New `*.py` files**: add them to BUILD targets as you create them. New tests
in `tests/` need their own `py_test` (mirror neighbors: `conftest.py` in `srcs`
+ `imports = ["."]` where siblings use them). `robodog_client/capture.py` is a
standalone host tool — it stays exempt (do not invent a test for it).

### 2.4 Display-Change Loop (Renderer/UI/pictures/data tasks)

When the task changes what the panel draws:

```bash
# 1. real compile
bazel run //robodog_client:build_firmware
# 2. flash the board (host esptool — the sanctioned path)
bazel run //robodog_client:flash_firmware -- --port "${ROBODOG_PORT:-/dev/ttyACM0}"
# 3. capture + vision readback (sync lands ~40 s post-boot; --n 3 covers it)
python3 robodog_client/capture.py --port "${ROBODOG_PORT:-/dev/ttyACM0}" \
  --reset --n 3 --timeout 120 --describe --out "$ARTIFACTS_DIR/robodog-shots"
```

Read `frameN_analysis.txt` (vision transcription) + `panel_landscape.png`. The
final frame should show the header date/time (WiFi + sync healthy) and the
expected rows/status words. A BLANK header + `no wifi` is the expected pre-sync
boot frame, not a pass. If the intended state is not on the panel, iterate.

### 2.5 Track Progress

```
Task 1: UPDATE robodog_client/main/robodog.c ✅ (wifi + gRPC sync healthy in frame)
Task 2: CREATE tests/test_foo.py ✅
```

### 2.6 Handle Deviations

1. Document WHAT changed, WHY, then continue.

**PHASE_2_CHECKPOINT (per task):**
- [ ] Task implemented
- [ ] Host build-check passes
- [ ] Firmware build passes (when main/ or components/ changed)
- [ ] Display-affecting task shows its state in a captured frame
- [ ] Progress logged; deviations documented

---

## Phase 3: TESTS - Write and Run Host Tests

### 3.1 Requirements

- Wire/proto: run `lma_proto_selftest` (host-only):
  `cc -std=c11 -Wall -Wextra robodog_client/main/lma_proto_selftest.c robodog_client/main/lma_proto.c -o /tmp/lma_selftest && /tmp/lma_selftest`
- Pictures: keep `tests/test_calendar_pics.py` green; add cases for new keys/boxes.
- Every new/changed host function gets a test mirroring `tests/` patterns.

### 3.2 Write Tests (happy path, edge cases, error cases)

### 3.3 Run Tests

```bash
bazel test //tests:all --test_tag_filters=-requires_hardware --test_output=errors
```

If a test fails: determine whether the implementation or the test is wrong, fix
the root cause, re-run.

**PHASE_3_CHECKPOINT:**
- [ ] Tests written for new code
- [ ] All host tests pass

---

## Phase 4: ARTIFACT - Write Implementation Progress

Write to `$ARTIFACTS_DIR/implementation.md`:

```markdown
# RoboDog Implementation Progress

**Generated**: {YYYY-MM-DD HH:MM}
**Workflow ID**: $WORKFLOW_ID
**Status**: {COMPLETE | IN_PROGRESS | BLOCKED}

## Tasks Completed
| # | Task | File | Status | Notes |
|---|------|------|--------|-------|
| 1 | {description} | `robodog_client/main/x.c` | ✅ | panel-captured |

## Files Changed
| File | Action | Lines |

## Tests Written
| Test File | Test Cases |

## Captured-Frame Evidence (display changes)
| Frame | Vision readback (verbatim) | Linux found |
|-------|-----------------------------|--------------|
| frame1 | `2026-10-15 21:25 / RoboDog play date 14:00 / BLANK` | header clock + row set OK |

## Deviations from Plan
{what / why / continued}

## Build Status
- [x] `bazel build //...` passes after all changes
- [x] `bazel run //robodog_client:build_firmware` passes
- [x] Unit tests pass ({N})

## Issues Encountered
{problem → resolution}
```

---

## Success Criteria

- **ALL_TASKS**: every plan task executed; no stubs/placeholders
- **HOST_GREEN**: bazel build //..., ruff, mypy, unit tests pass
- **FW_GREEN**: firmware builds via ESP-IDF Docker when main/ or components/ changed
- **PANEL_EVIDENCE**: display-affecting tasks ended with a captured frame whose
  vision readback shows the intended state (recorded in implementation.md)
- **ARTIFACT_WRITTEN**: `$ARTIFACTS_DIR/implementation.md` documents all results
- **HAZARDS**: esptool never touched the Cardputer/RNode; robodog flashed only
  via `flash.sh`
