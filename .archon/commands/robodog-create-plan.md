---
description: Create comprehensive implementation plan for RoboDog display work — every render/UI/data change must be validated from the panel pixels (screen capture + vision readback), never a re-render. Bazel-native, robodog hardware safety rules baked in.
argument-hint: <feature description | path/to/prd.md>
---

# RoboDog Create Implementation Plan

**Input**: $ARGUMENTS
**Workflow ID**: $WORKFLOW_ID

---

## ⚠️ RoboDog Project Rules (MANDATORY — from AGENTS.md + robodog_client/README.md)

**Hardware safety — violating these can brick physical devices:**

1. **The robodog board is the Waveshare ESP32-S3 RLCD-4.2 (ST7305)** — verified
   fingerprint VID `0x303A` PID `0x1001` (plain ESP32-S3 USB-Serial-JTAG; see
   `lma_core/device_detect.py`). It is flashed ONLY via
   `bazel run //robodog_client:flash_firmware -- --port <port>`
   (`robodog_client/flash.sh` runs host esptool because Docker ttyACM RTS
   passthrough breaks the button-free reset).
2. **NEVER run esptool on the Cardputer** (`0x303A:0x8120`) — it kills the
   USB-Serial-JTAG interface; recovery needs a physical replug. The Cardputer is
   flashed only via `bazel run //cardputer_client:flash`. A robodog task never
   touches the Cardputer's port.
3. **NEVER flash the RNode via esptool** (`/dev/ttyUSB*`) — only the web flasher
   at https://flasher.rnode.network/. An interrupted esptool flash bricks it.
4. The robodog board is the DEV/kiosk display — after any flash session the new
   firmware owns the panel (unlike the Cardputer there is no "production client"
   to restore). A plan that changes rendering must therefore make the ON-PANEL
   result the acceptance evidence.

**Verification is pixel-driven (the one rule that matters):**
- Every display/UI change (`robodog_client/main/robodog.c`, `lma_proto.*`,
  font/layout/pins) is validated from the **exact panel pixels**, never a
  re-render and never the model's guess. The capture hook is compiled into
  every build — `capture.py` streams the live u8g2 buffer over USB-JTAG and
  reconstructs `panel_raw_portrait.png` (300×400) / `panel_landscape.png`
  (400×300). `--describe` sends the frame to the self-hosted vision model
  (Qwen2.5-VL, `spark-eebf`, `~/.hermes/config.yaml "vision"`) which transcribes
  the header clock + every row. The agent reasons from THAT transcription.
- Host-testable units: `main/lma_proto.{c,h}` (dependency-free protobuf codec,
  host-tested via `lma_proto_selftest.c`), the picture pipeline
  (`lma_core/calendar_pics.py` + `tools/calendar_pics.py`, covered by
  `tests/test_calendar_pics.py`), and the calendar data path
  (`lmao_server` gRPC `lma.LMAO/GetCalendar`).

**Build rules:**
- **Bazel is the canonical build** (v7.4.1 via bazelisk). Every new `*.py` in
  the plan MUST include a BUILD target change — a test without a `py_test`
  target silently never runs (issue #87). `robodog_client/capture.py` is a
  host-run standalone tool (exempt, like `tools/archon_webhook_relay.py`).
- Firmware builds via the ESP-IDF Docker image — `bazel run //robodog_client:build_firmware`
  (ESP-IDF ≥ 5.1; the u8g2 + u8g2_st7305 components are FETCHED by
  `u8g2_prepare.sh` into gitignored `components/`, never vendored — plans must
  not copy upstream sources into the repo).
- Lint: `ruff check` / `ruff format --check`; type-check: `mypy`; run on changed
  files only. Vendored code (u8g2/csrc, components/) is lint-exempt.
- Dependencies are pinned in `lmao_server/requirements_lock.txt` — catalogue
  versions from there, not package.json (there is none).

---

## Your Mission

Transform "$ARGUMENTS" into a battle-tested implementation plan through
systematic codebase exploration, pattern extraction, and strategic research.

**Core Principle**: PLAN ONLY - no code written. Create a context-rich document
that enables one-pass implementation success.

**Execution Order**: CODEBASE FIRST, RESEARCH SECOND. Solutions must fit existing
patterns before introducing new ones.

**Output**: `$ARTIFACTS_DIR/plan.md`

---

## Phase 0: DETECT - Input Type Resolution

### 0.1 Determine Input Type

| Input Pattern | Type | Action |
|---------------|------|--------|
| Ends with `.prd.md` | PRD file | Parse PRD, select next phase |
| Ends with `.md` and contains "Implementation Phases" | PRD file | Parse PRD, select next phase |
| File path that exists | Document | Read and extract feature description |
| Free-form text | Description | Use directly as feature input |
| Empty/blank | Error | STOP - require input |

### 0.2 If PRD File Detected

1. **Read the PRD file**
2. **Parse the Implementation Phases table** - find rows with `Status: pending`
3. **Check dependencies** - only select phases whose dependencies are `complete`
4. **Select the next actionable phase** (first pending with all deps complete)
5. **Extract phase context**: PHASE, GOAL, SCOPE, SUCCESS SIGNAL, PRD CONTEXT

### 0.3 If Free-form Description

Proceed directly to Phase 1 with the input as feature description.

**PHASE_0_CHECKPOINT:**
- [ ] Input type determined
- [ ] If PRD: next phase selected and dependencies verified
- [ ] Feature description ready for Phase 1

---

## Phase 1: PARSE - Feature Understanding

### 1.1 Discover Project Structure

**CRITICAL**: Do NOT assume `src/` exists. Discover actual structure:

```bash
ls -la
ls -la robodog_client/ robodog_client/main/      # firmware layout
ls -la lma_core/ tools/ tests/ lmao_server/      # host + server layout
git log --oneline -10 master                     # recent conventions
```

### 1.2 Categorize the Request

Determine which slice(s) the request touches — this drives the plan's
verification tasks:

| Slice | Touches | Primary verification |
|-------|---------|---------------------|
| **Renderer/UI** | `robodog_client/main/robodog.c`, `config.h`, fonts, u8g2 usage | Panel capture + vision readback (mandatory) |
| **Wire/proto** | `robodog_client/main/lma_proto.{c,h}` | Host `lma_proto_selftest` + live gRPC round-trip |
| **Pictures** | `lma_core/calendar_pics.py`, `tools/calendar_pics.py` | `tests/test_calendar_pics.py` + panel capture |
| **Server/data** | `lmao_server/*`, `lma_core/*` (GetCalendar, calendar storage) | Unit tests + robodog gate capture row set |
| **Pins/board** | `main/config.h`, `main/CMakeLists.txt`, `components/**` | Panel capture confirms display still drives |

Record the slice(s) in the plan — they decide which gate checks are load-bearing.

---

## Phase 2: RESEARCH - Codebase Analysis (CODEBASE FIRST)

Use the Task tool with subagent_type="Explore" for codebase intelligence
gathering. Mandatory reads for robodog work:

1. **`robodog_client/README.md`** — board, pins, wire contract, capture/validation
   loop, status hints (`no wifi` / `stale` / `offline`).
2. **`robodog_client/capture.py`** — how to extract frames, `--describe` vision
   readback, `--i1` offline decode, orientation auto-detection.
3. **`robodog_client/main/robodog.c`** — the render loop: sync → decode →
   merge δ → render → light-sleep. Understand where your change lands in it.
4. **`robodog_client/main/lma_proto.{c,h}`** — the protobuf codec (no malloc, no
   lwip — host-testable).
5. **`robodog_client/BUILD`** — `build_firmware` / `flash_firmware` targets; the
   patching context for new `*.py`/BUILD wiring.
6. **`lma_core/calendar_pics.py` + `tests/test_calendar_pics.py`** — the picture
   pipeline contract (pixel box layout) and its host tests.
7. **`lmao_server`** — the `CalendarBundle` source the panel renders.

Mirror existing patterns exactly — never introduce a second convention.

---

## Phase 3: STRATEGY - Capture-Driven Task Decomposition

Every task that changes what the panel DRAWs or SHOWS must include its own
"verify on the panel" sub-task, not just a unit test. Bake these into the plan's
task list:

- **If Renderer/UI slice**: a task to rebuild (`bazel run
  //robodog_client:build_firmware`), flash
  (`bazel run //robodog_client:flash_firmware -- --port <port>`), capture
  (`python3 robodog_client/capture.py --port <port> --reset --n 3 --timeout 120
  --describe --out $ARTIFACTS_DIR/robodog-shots`), and assert the vision
  readback shows the intended state (header + rows/status words).
- **If Wire/proto slice**: a task to run the host selftest
  (`cc -std=c11 -Wall -Wextra robodog_client/main/lma_proto_selftest.c
  robodog_client/main/lma_proto.c -o /tmp/lma_selftest && /tmp/lma_selftest`)
  and confirm a live sync renders real `CalendarBundle` rows on the panel.
- **If Pictures slice**: normalize/regenerate pics via
  `bazel run //tools:calendar_pics -- --key <key> ...`, update
  `tests/test_calendar_pics.py`, and confirm the icons render on the panel.
- **Always**: the acceptance checklist must include the pixels, e.g. "final
  captured frame's vision readback shows the header date/time and the expected
  row set (no `no wifi`/`stale` unless that is the feature under test)."

Known board facts to weave in: WiFi is 2.4 GHz-only with NVS/builtin creds
(`wifi_ssid`/`wifi_pass`, fallbacks in `config.h`); sync lands ~40 s post-boot;
title clipping and icon boxes are tuned for current data; row count is a fixed
3-event cap (headline + 2 compact rows, 4th+ dropped by design).

---

## Phase 4: OUTPUT - Write the Plan

Write `$ARTIFACTS_DIR/plan.md` with:

1. **Goal & Scope** — problem, user, hypothesis, explicit non-goals.
2. **Categorized slices** — from Phase 1.2, with the per-slice verification.
3. **Research findings** — files, patterns, the exact functions/functions to touch.
4. **Task list** — ordered, each with: file to CREATE/UPDATE, pattern to mirror,
   GOTCHA warnings, its "verify on the panel or host unit" sub-task, and the
   build-check (`bazel build //...`, firmware build when main/ changes).
5. **Test plan** — host units (`lma_proto_selftest`, `tests/test_calendar_pics.py`)
   + the mandatory panel-capture acceptance (exact `capture.py --describe`
   command and the expected readback).
6. **Risks** — e.g. frame/width tuning drift on real data, WiFi reachability,
   NVS persisted event cache, light-sleep timing.

**PHASE_4_CHECKPOINT:**
- [ ] Plan written to `$ARTIFACTS_DIR/plan.md`
- [ ] Every display-affecting task has a panel-capture verification step
- [ ] BUILD target changes called out for new `*.py`
- [ ] No vendored upstream sources proposed

---

## Success Criteria

- **PLAN_WRITTEN**: `$ARTIFACTS_DIR/plan.md` exists and is context-rich
- **CAPTURE_FIRST**: every Renderer/UI/pictures/data task names its panel-capture
  (or host-unit) verification explicitly
- **CONVENTIONS**: plan mirrors existing robodog patterns; no second convention
- **HAZARDS**: no esptool on Cardputer/RNode; robodog flash only via flash.sh
