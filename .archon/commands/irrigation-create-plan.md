---
description: Create the implementation plan for a smart-irrigation feature or for committing the algorithm-evaluation deliverables. Blueprint + evaluation aware, Bazel/MicroPython-native.
argument-hint: <feature description | phase from the blueprint | "Phase 0: algorithm evaluation">
---

# Smart Irrigation — Create Implementation Plan

**Input**: $ARGUMENTS
**Workflow ID**: $WORKFLOW_ID

## ⚠️ Project rules (MANDATORY)

Read and obey `smart_irrigation/AGENTS.md` and `/home/pondycrane/LMAO/AGENTS.md`:

- Never esptool-probe/flash any device; no ad-hoc serial sessions; leave the
  production Cardputer running.
- MicroPython only on the Atom Lite; fixed-point Q8.8 (no floats if avoidable);
  control engine ≤ 30 KB RAM; I2C 100 kHz; deep sleep + watchdog + heap recovery.
- Every new `*.py` gets a Bazel target (`py_library`/`py_test` for host-testable
  logic; `exports_files`/`filegroup` for MicroPython entry files; `filegroup`
  only for vendored `firmware/lib/urns/**`).
- Pump safety overrides are hard-coded and fail-off.

---

## Your Mission

PLAN ONLY — no implementation. The plan must be executable top-to-bottom by a
fresh agent using only the plan + repo.

**Read before planning**:
- `$ARTIFACTS_DIR/assessment.md` (and `.json`) — the workflow's classification
  of this request (`mode`, `phase`, `touches_firmware`, prerequisites).
- `$ARTIFACTS_DIR/evaluation.md` — the algorithm evaluation. In `development`
  mode the verdict **is** the architecture: plan the verdict's design, not the
  blueprint's sketch. In `evaluation` mode this artifact is the deliverable
  draft you are planning to commit.
- The blueprint: `smart_irrigation/docs/development-plan.md` (fallback
  `/home/pondycrane/smart-irrigation-dev-plan.md`) — read the sections for the
  selected phase plus §3 (sensor map), §4/§13 (algorithm), §7 (payload),
  §9 (constraints), §11 (layout), Appendix B/C.
- Existing patterns: `cardputer_client/main.py`, `config.py`,
  `lora_boards.py`, `flash.py`, `cardputer_client/BUILD`,
  `cardputer_client/proto/lma_encoder.py`, `tests/BUILD`.

**Output**: `$ARTIFACTS_DIR/plan.md`.

---

## Phase 1: CLASSIFY THE REQUEST

From `assessment.md` and `$ARGUMENTS`:

| Field | Value |
|-------|-------|
| Mode | evaluation \| development |
| Blueprint phase | evaluation \| 1–8 \| cross-cutting |
| Components | firmware \| stm32-dtu \| server \| scripts \| docs \| tooling |
| Hardware needed at E2E | Atom Lite \| Cardputer \| DTU \| none |

If the assessment says `touches_firmware=true`, the plan MUST read and follow
`docs/algorithm-evaluation.md` (the workflow preflight guarantees it exists).
If it does not exist, STOP with an error — do not plan firmware.

### Mode = evaluation

Plan the **commit of the evaluation deliverables**:
- `smart_irrigation/docs/algorithm-evaluation.md` (from `$ARTIFACTS_DIR/evaluation.md`)
- Reference assets from `$ARTIFACTS_DIR/evaluation-assets/` per its `MANIFEST.md`
  (e.g. `smart_irrigation/scripts/sim_control.py`,
  `smart_irrigation/scripts/batch_encoder.py`) with BUILD targets + tests
- Update `smart_irrigation/docs/development-plan.md` checkboxes for completed items
  and, when the verdict supersedes the sketch, add a short "evaluation verdict"
  pointer at the top.
- No firmware code in this plan.

### Mode = development

Plan exactly the requested feature/phase. Do not pull in later phases. If the
requested work depends on an earlier unfinished phase, list that as a
prerequisite in the plan (and `NOT Building`).

---

## Phase 2: EXPLORE THE CODEBASE

Use a thorough Explore pass (or equivalent direct exploration) and record
**actual file:line patterns**, not generic examples:

1. Similar implementations in `cardputer_client/` (client loop, LXMF send/receive,
   config structure, flash tool, BUILD wiring, host-test patterns).
2. MicroPython constraints in the existing `urns` port (how imports are guarded,
   `machine`/`micropython` shims, mocks used by `tests/test_urns_*.py`).
3. The protobuf wire encoder (`lma_encoder.py`) — exact functions to reuse for
   SensorReport/CommandRequest/CommandAck; the `sensor_id` map.
4. The server ingest path (`k8s-app/iot_ingest.py`, `lma_core/`) for any
   server-side change.
5. `tests/BUILD` conventions: `conftest.py` in `srcs`, `imports = ["."]`,
   `requires_hardware` tags, data deps.
6. Whether `smart_irrigation/` already has the directory/target the feature
   needs.

**CHECKPOINT**: ≥3 real patterns with file:line refs; integration points mapped.

---

## Phase 3: RESEARCH (only after Phase 2)

Look up exact docs for: the chosen algorithm/paper, MicroPython modules used
(`machine.I2C/ADC/UART/WDT`, `esp32`), PCA9548A/SHT30/QMP6988 register maps,
SX1262/UART DTU bridge specifics, LoRaWAN class-A/duty-cycle limits, Bazel
rules_python patterns. Cite versions where relevant. Prefer `.mpy`/MicroPython
docs over CPython docs.

---

## Phase 4: DESIGN THE CHANGE

For every file, decide CREATE vs UPDATE, and specify:

- **Firmware host-testable logic** (control engine, calibration, protocol
  encode/decode, pressure trend) → pure module + `py_test` with MicroPython
  mocks. Must be importable on CPython (guard `machine` imports like the
  existing client does).
- **Firmware device-only files** (`main.py`, `boot.py`, `config.py`,
  `lora_boards.py`, `flash.py`, vendored lib copies) → `exports_files`/`filegroup`,
  plus BUILD `data` deps for the flash/E2E tooling.
- **Vendored copies** (blueprint §2.1) → keep byte-identical to
  `cardputer_client/`; never edit the copy to "fix" style. Record the source
  commit/hash in a comment or `VENDORED.md`.
- **Safety overrides** → hard-coded, fail-off, unit-tested (list each one).
- **Sensor IDs/protocol** → preserve wire compatibility; add a round-trip test.
- **Server-side** → `iot_ingest.py` changes with DuckDB schema migration
  (`irrigation_sensor_readings` per blueprint §7.2) + tests.
- **STM32 DTU** → PlatformIO sources only, build-verifiable, marked
  hardware-UNVERIFIABLE unless a DTU is attached.

Also plan the **E2E verification** for the phase: which Bazel test target
proves it (existing or new), what it observes (server log / DuckDB / REPL), and
what "loud skip" means when the hardware is absent.

---

## Phase 5: WRITE `$ARTIFACTS_DIR/plan.md`

Use this exact structure (downstream `lmao-plan-setup` parses these headings):

```
# Feature: {name}

## Summary
{2–4 sentences}

## Metadata
| Field | Value |
|-------|-------|
| Blueprint phase | {n / evaluation / cross-cutting} |
| Mode | {evaluation / development} |
| Complexity | LOW/MEDIUM/HIGH |
| Hardware E2E | {what must be attached / "none"} |

## Mandatory Reading
| File | Why |
|------|-----|
| smart_irrigation/AGENTS.md | project rules |
| docs/algorithm-evaluation.md | {verdict + sections this feature must follow} |
| {blueprint section} | {phase spec} |

## Patterns to Mirror
| Category | File:Lines | Pattern |
|----------|------------|---------|
| ... | ... | ... |

## Files to Change
| Action | Path | Purpose |
|--------|------|---------|
| CREATE | smart_irrigation/... | ... |
| UPDATE | smart_irrigation/... | ... |

## NOT Building (Scope Limits)
- {explicit exclusions with why}

## Step-by-Step Tasks
### Task 1: {CREATE/UPDATE} `{path}`
- ACTION / IMPLEMENT / MIRROR / IMPORTS / GOTCHA / VALIDATE

{...}

## Testing Strategy
| Test file | Cases | Validates |
|-----------|-------|-----------|
| ... | ... | ... |

## Validation Commands
### Level 1: STATIC_ANALYSIS
```bash
bazel build //... && ruff check <changed-files> && mypy <changed-files>
```
### Level 2: UNIT_TESTS
```bash
bazel test //tests:<targets> --test_output=errors
```
### Level 3: FULL_SUITE
```bash
bazel test //tests:all --test_tag_filters=-requires_hardware --test_output=errors
```

## Acceptance Criteria
- [ ] ...

## Risks and Mitigations
| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|

## Notes
{assumptions from the blueprint that this feature validates on hardware}
```

Every task must have an executable `VALIDATE` command. No placeholders.

---

## Phase 6: VERIFY THE PLAN

- [ ] Mode/phase matches the assessment; no phase creep
- [ ] Firmware plans follow the evaluation verdict (not the blueprint sketch
      when they differ); fallback behavior stated
- [ ] Every new `.py` file has its BUILD wiring designed
- [ ] Every safety override appears as a task + test
- [ ] Hardware E2E explicitly planned (target, observations, skip semantics)
- [ ] Blueprint assumptions that this feature validates are flagged in Notes
- [ ] "No prior knowledge" test: a fresh agent could execute using only the plan

If verification fails, fix the plan before output.

---

## Phase 7: OUTPUT

```markdown
## Plan Created ✅

**File**: `$ARTIFACTS_DIR/plan.md`
**Mode**: {evaluation|development} · **Phase**: {n}
**Files**: {C} create, {U} update · **Tasks**: {T}
**Hardware E2E**: {plan}
**Confidence**: {1–10}/10 — {rationale}
Next: plan setup.
```

## Success Criteria

- **PLAN_WRITTEN**: `$ARTIFACTS_DIR/plan.md` complete, self-contained
- **EVALUATION_FIRST**: firmware plans are grounded in `docs/algorithm-evaluation.md`
- **BAZEL_WIRED**: BUILD wiring planned for every new file
- **SAFETY_PLANNED**: pump/electrical safety overrides planned and testable
- **NO_IMPLEMENTATION**: no repo files changed by this command
