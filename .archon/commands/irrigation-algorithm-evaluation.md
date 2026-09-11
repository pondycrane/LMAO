---
description: Mandatory algorithm evaluation (blueprint Appendix 13) — verdict + rulebase/ET0 + ML schema + protocol + risks, with benchmark evidence. Fast-passes when already delivered.
argument-hint: (no arguments - reads $ARTIFACTS_DIR/assessment.md and the development blueprint)
---

# Smart Irrigation — Algorithm Evaluation (Blueprints Appendix 13)

**Workflow ID**: $WORKFLOW_ID

## ⚠️ Project rules (MANDATORY)

Read and obey `smart_irrigation/AGENTS.md` and `/home/pondycrane/LMAO/AGENTS.md`.
Hardware safety: never use esptool for probing/flashing any device; never open
ad-hoc serial sessions; leave the production Cardputer running.

---

## Your Mission

The blueprint (Appendix 13, "Challenge Brief — Algorithm Evaluation Mandate")
forbids writing firmware before this evaluation exists. Produce the evaluation
document with **real benchmark evidence**, or fast-pass when it is already
delivered.

This command does **analysis + reference tooling**, not firmware. It writes to
`$ARTIFACTS_DIR` only. The downstream plan/implement phases commit the
deliverable into `smart_irrigation/docs/algorithm-evaluation.md`.

**Output artifacts**:
- `$ARTIFACTS_DIR/evaluation.md` — the full deliverable (structure below)
- `$ARTIFACTS_DIR/evaluation-evidence.md` — raw benchmark numbers, tables, methodology
- `$ARTIFACTS_DIR/evaluation-assets/` — reference implementations + a `MANIFEST.md`
  (batch_encoder.py, benchmark harness, chosen-algorithm reference module,
  response-surface generator) for the implement phase to commit

---

## Phase -1: LOCATE THE RUN TREE (MANDATORY)

Your shell may start in the main checkout. Locate this run's worktree before
reading/writing repo files:

```bash
git worktree list --porcelain | grep -E "^(worktree|branch)"
```

Use the worktree for this run (typically
`~/.archon/workspaces/*/worktrees/archon/task-*`); if this is a
`--no-worktree` run, use the current checkout. Set it once:

```bash
WT=/absolute/path/to/run-tree
cd "$WT"
```

Blueprint path resolution (prefer the in-repo copy, fall back to the host copy):

```bash
BLUEPRINT="smart_irrigation/docs/development-plan.md"
[ -f "$BLUEPRINT" ] || BLUEPRINT="/home/pondycrane/smart-irrigation-dev-plan.md"
```

---

## Phase 0: FAST-PASS — Evaluation Already Delivered

```bash
ASSESS="$ARTIFACTS_DIR/assessment.json"
MODE=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('mode','development'))" "$ASSESS" 2>/dev/null || echo development)
if [ -f "smart_irrigation/docs/algorithm-evaluation.md" ]; then
  echo "FAST-PASS: smart_irrigation/docs/algorithm-evaluation.md exists (mode=$MODE)"
fi
```

If fast-pass prints, write a short `$ARTIFACTS_DIR/evaluation.md` that says the
evaluation is already present, points at the file, and lists its sections, then
output exactly:

```markdown
## Algorithm Evaluation ✅ (fast-pass — deliverable already exists)
```

and STOP. Do not re-run benchmarks.

Otherwise continue. (If `mode=development` and the file is missing, the
workflow preflight has already failed before this node — you should not see
that case.)

---

## Phase 1: LOAD — Blueprint + Codebase Reality

1. Read the **full blueprint**, especially: §3 sensor mapping, §4 fuzzy engine,
   §7 payload budget, §9 handoff notes/constraints, §13 challenge brief,
   Appendix B pin map, Appendix C rule matrix template.
2. Read the existing LMAO patterns you must reuse:
   - `cardputer_client/main.py`, `cardputer_client/config.py`,
     `cardputer_client/lora_boards.py` — client structure,
   - `cardputer_client/lib/urns/` — the µReticulum MicroPython port,
   - `cardputer_client/proto/lma_encoder.py` — protobuf wire format,
   - `k8s-app/iot_ingest.py` — server-side ingest to extend,
   - `lma_core/` — shared device/identity helpers.
3. Note the blueprint's own unknowns (its §9) and treat pin/board details as
   assumptions to be validated on hardware.

**CHECKPOINT**: blueprint read, reuse targets identified, unknowns listed.

---

## Phase 2: BENCHMARK — Actually Compare the Four Approaches (A)

Build a small, honest simulation harness under
`$ARTIFACTS_DIR/evaluation-assets/bench/` and run it. Do **not** hand-wave:
produce numbers.

**Plant model** (synthetic but physically motivated): soil moisture bucket
θ(t) with evapotranspiration ET₀(T, RH) and irrigation input; generate several
scenarios — hot/dry day, mild day, rain approaching (falling pressure),
sandy vs clay soil, sensor noise (±3% moisture, ±0.5 °C, ±1 hPa), and a
stuck-pump (no moisture response) case.

**Controllers to implement and compare**:

| # | Approach | Notes |
|---|----------|-------|
| 1 | Threshold state machine with hysteresis | baseline, zero memory |
| 2 | PID on soil moisture | anti-windup + actuator time constraints |
| 3 | Fuzzy (blueprint §4 sketch, Q8.8) | faithful to the sketch |
| 4 | Model-based / hybrid ET₀ + decay predictor | Per blueprint §C, + optional fuzzy/rule overrides |

**Metrics** (per scenario, aggregate): water used, time spent below target
band, time above field capacity (over-watering), number of pump cycles,
worst-case overshoot, robustness to a 2× soil-coefficient mismatch, estimated
RAM/CPU cost (simulate the Q8.8 rule evaluation and measure), calibration
inputs required from the user.

**Pressure-trend sensitivity (mandatory)**: with the fuzzy/rule layer, sweep
pressure trend from −3 to +3 hPa/h and report the max % change in pump
duration. If < 10% in every case, recommend dropping pressure trend as a
control input (keep it as an ML feature) and say so in the verdict.

**Record** the harness, scenario definitions, and raw results. Save every
number into `$ARTIFACTS_DIR/evaluation-evidence.md`.

**CHECKPOINT**: 4 controllers × ≥4 scenarios benchmarked with numbers; pressure
sensitivity measured; harness saved.

---

## Phase 3: DELIVERABLE — Write `$ARTIFACTS_DIR/evaluation.md`

Use exactly these sections; every claim must trace to Phase 2 evidence or a
cited source (library docs, datasheet) — no invented specs.

### 1. Algorithm verdict
Pick **one**: fuzzy, model-based/hybrid, state machine, or PID. Justify with the
Phase 2 table across: decision accuracy, water use, pump cycles, memory
footprint, calibration effort, robustness, suitability as an ML training
policy. If the verdict is not fuzzy, say explicitly that the blueprint's §4
fuzzy engine is superseded and what replaces it. If hybrid, define precisely
which layer does what.

### 2. Rulebase / membership functions (if fuzzy or hybrid)
- Compress the blueprint's 5×4×3 matrix into explicit rules (group equal
  consequents). Output a machine-readable table (Python dict / JSON) suitable
  for embedding in firmware, in **Q8.8 integers**.
- Derive membership-function breakpoints from the soil physics reasoning in
  §13.B, not arbitrary numbers; document the soil-type calibration factor.
- Defuzzification: state whether Sugeno-style weighted average
  (`Σ(wᵢ·cᵢ)/Σwᵢ`) is used and why (division cost).
- Include the response-surface data (grid CSV) and the sensitivity analysis.

### 3. ET₀ model (if model-based or hybrid)
- Give the simplified equation actually used (Hargreaves-Samani or
  Penman-Monteith reduced to T + RH + pressure), with units and the
  calibration coefficient.
- Reference implementation, fixed-point strategy for MicroPython, and its
  host-testable Python twin.

### 4. ML dataset schema (D)
- The batched 30-minute training sample format (the JSON example in §D) and the
  on-node derived features (Δθ/min, ΔT/min, ΔH/min, ΔP/min, last watering
  time/duration, watering trace tagging).
- `batch_encoder.py` reference (in `evaluation-assets/`) that converts raw
  per-sample records into batched training rows, with a small self-test.

### 5. Protocol recommendation (E)
Compare **full µReticulum/LXMF on-node** vs the **minimal UART binary** frame
protocol from §E. Estimate firmware size for each (use real file sizes from
`cardputer_client/lib/urns/`), flash/RAM, CPU per message, and development
risk. Recommend one and state what that means for Phase 1 and Phase 6 of the
blueprint. If minimal protocol: define the frame layout, TYPE values, CRC16,
and how the server reconstructs the LXMF envelope.

### 6. Risk assessment
Rank the top risks (soil-type mismatch, pressure-trend false rain detection,
pump dynamics / no moisture response, I2C lockup, battery/solar, duty cycle,
calibration drift) with likelihood, impact, detection signal, and mitigation.

### 7. Recommended development plan
An optimized, ordered phase plan derived from the verdict. Start from the
blueprint's Phases 1–8 and correct them (e.g. port only the needed µReticulum
subset if the minimal protocol wins). Include explicit hardware prerequisites
and the E2E verification for each phase.

### Final section: reference assets manifest
List every file in `evaluation-assets/` with a one-line purpose and the
proposed destination path inside the repo (e.g.
`smart_irrigation/scripts/sim_control.py`, `smart_irrigation/scripts/batch_encoder.py`).

---

## Phase 4: VERIFY

- [ ] All 7 mandated deliverables present, each with evidence or a cited source
- [ ] Response surface / sweep data exists (CSV or table in the artifact)
- [ ] Rule matrix (if fuzzy/hybrid) is machine-readable and Q8.8
- [ ] Safety overrides defined as hard overrides (not fuzzy)
- [ ] `evaluation-assets/MANIFEST.md` complete
- [ ] No firmware code was written to the repo by this command

---

## Phase 5: OUTPUT

```markdown
## Algorithm Evaluation ✅

**Verdict**: {fuzzy | model-based/hybrid | state machine | PID}
**Evidence**: $ARTIFACTS_DIR/evaluation-evidence.md
**Deliverable draft**: $ARTIFACTS_DIR/evaluation.md
**Assets**: $ARTIFACTS_DIR/evaluation-assets/ ({N} files)
**Protocol**: {full µReticulum/LXMF | minimal UART binary}
Next: plan the evaluation commit (`create-plan`).
```

## Success Criteria

- **EVIDENCE_BASED**: every controller compared with measured numbers on the same scenarios
- **VERDICT_MADE**: one approach recommended, blueprint sketch explicitly adopted or superseded
- **PRESSURE_SENSITIVITY**: measured and acted on (<10% ⇒ drop as control input)
- **ASSETS_READY**: reference implementations + manifest ready for the implement phase
- **NO_FIRMWARE**: no repo firmware files written by this command
