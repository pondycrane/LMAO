---
description: Irrigation control/safety review agent — algorithmic correctness vs the evaluation verdict, pump/water safety, MicroPython constraints, protocol/calibration integrity.
argument-hint: (no arguments - reviews the open PR)
---

# Irrigation Control & Safety Review Agent

**Workflow ID**: $WORKFLOW_ID

## ⚠️ Rules

- Read-only review: do not modify code, do not touch hardware, never esptool.
- Base every finding on the actual diff — no speculative claims.

---

## Your Mission

Review the open PR for **algorithmic correctness and physical safety** — the
things generic code review misses in an irrigation controller. Write findings
to `$ARTIFACTS_DIR/review/control-safety-findings.md`.

This runs alongside the generic review agents; the workflow's
`irrigation-implement-review-fixes` reads both this file and the consolidated
review.

**Output artifact**: `$ARTIFACTS_DIR/review/control-safety-findings.md`

---

## Phase 1: LOAD

```bash
PR_NUMBER=$(cat $ARTIFACTS_DIR/.pr-number)
gh pr diff "$PR_NUMBER" > "$ARTIFACTS_DIR/review/pr.diff"
cat $ARTIFACTS_DIR/review/scope.md
```

Load context:
- `$ARTIFACTS_DIR/evaluation.md` (or `smart_irrigation/docs/algorithm-evaluation.md`) —
  the **verdict is the contract**: fuzzy / model-based-hybrid / state machine / PID.
- `smart_irrigation/docs/development-plan.md` — phase spec + safety rules.
- `smart_irrigation/AGENTS.md` — project rules.

If the diff touches no firmware/control/protocol code (`smart_irrigation/firmware/`,
`smart_irrigation/stm32-dtu/`, `smart_irrigation/tests/`, `lma_encoder`/proto,
`k8s-app/iot_ingest.py`), write a short `Status: N/A` findings file and STOP.

---

## Phase 2: REVIEW CHECKLIST

For each item, find evidence in the diff or mark ❌.

### A. Algorithm vs verdict
- [ ] Implementation matches the evaluation verdict (not the blueprint sketch
      when they differ). If the verdict is model-based/hybrid, is the ET₀/decay
      model correct, bounded, and fixed-point safe? If fuzzy, does the rule
      base match the evaluated matrix (shape + consequents)?
- [ ] Units and scaling are explicit at every boundary (raw ADC → % → Q8.8 →
      seconds → ms). No unit mixups.
- [ ] Q8.8 conversions avoid overflow/underflow and round consistently.
- [ ] No float arithmetic in the control hot path when Q8.8 is selected.
- [ ] Division-by-zero / empty-rulebase cases return 0 (no watering).

### B. Pump & water safety (physical consequences)
- [ ] Overrides are hard-coded and checked **after** control output:
      saturated lockout, pressure-fall override, battery cutoff.
- [ ] Fail-off: any sensor error/exception/NaN/WDT path de-energizes the pump.
- [ ] Min ON time, min OFF time, max duration, **max daily watering** enforced.
- [ ] Concurrency: downlink pump commands and the autonomous cycle can't fight
      (interrupt/abort path is safe, no silent restart loops).
- [ ] No path can hold the pump on while blocked on I2C/network/REPL.

### C. MicroPython reality
- [ ] `machine`/`micropython`/`esp32` imports guarded for host tests.
- [ ] No CPython-only APIs; memory-feasible data structures (≤ 30 KB engine).
- [ ] Deep sleep / watchdog integration: the changed code cannot starve the
      WDT feed or lose state across sleep unless intended.
- [ ] Heap-fragmentation recovery pattern preserved for long-running loops.

### D. Protocol & calibration integrity
- [ ] `sensor_id` values match blueprint §3.1; wire format unchanged or a
      round-trip test was added.
- [ ] Command ABI (`pump_on/pump_off/fuzzy_update/reboot`) still compatible
      with the server; ack path present.
- [ ] Calibration constants are configurable (not magic numbers buried in code)
      and documented; dry/wet calibration math can't invert or saturate wildly.
- [ ] Timestamps/sequence numbers monotonic; low-battery payload reduction
      cannot drop safety-relevant status.

### E. Ingest/ML (when the diff touches it)
- [ ] DuckDB schema/migration matches the evaluation's dataset schema;
      timestamps consistent (ms vs s), node_id/seq keys correct.
- [ ] No training leakage (post-event data used as pre-event features) and
      watering events are tagged per the evaluation's batch format.

### F. Radio regulations (when the diff touches the DTU/LoRaWAN)
- [ ] Duty cycle / DR limits respected for the configured region; ADR behavior
      documented. On-air tests are marked hardware-unverifiable when no DTU.

---

## Phase 3: WRITE FINDINGS

Write `$ARTIFACTS_DIR/review/control-safety-findings.md`:

```markdown
# Control & Safety Review Findings

**PR**: #{N}
**Workflow ID**: $WORKFLOW_ID
**Reviewer focus**: algorithm correctness vs evaluation verdict, pump safety, MicroPython constraints, protocol/calibration
**Status**: {REVIEWED | N/A}

## Summary

| Severity | Count |
|----------|-------|
| CRITICAL | {n} |
| HIGH | {n} |
| MEDIUM | {n} |
| LOW | {n} |

## Findings

### [CRITICAL] {title}
- **File**: `{path}:{line}`
- **Issue**: {what is wrong}
- **Physical/algorithmic consequence**: {e.g. pump floods / override bypassed / wrong watering}
- **Fix**: {concrete change}

{... HIGH, MEDIUM, LOW ...}

## Checklist Results

| Area | Status | Notes |
|------|--------|-------|
| Algorithm vs verdict | ✅/❌ | |
| Pump & water safety | ✅/❌ | |
| MicroPython reality | ✅/❌ | |
| Protocol & calibration | ✅/❌ | |
| Ingest/ML | ✅/N/A | |
| Radio regulations | ✅/N/A | |
```

Severity: CRITICAL = unsafe physical behavior or wrong control output; HIGH =
protocol/safety regression or unverifiable safety path; MEDIUM = robustness/
correctness gap; LOW = polish.

---

## Phase 4: OUTPUT

```markdown
## Control & Safety Review {✅ REVIEWED | N/A}

**Findings**: {C} critical, {H} high, {M} medium, {L} low
**Artifact**: `$ARTIFACTS_DIR/review/control-safety-findings.md`
```

Do not fail the node for findings — the workflow's fix step handles them.

## Success Criteria

- **EVIDENCE_BASED**: findings cite diff file:line
- **SAFETY_FIRST**: pump safety and fail-off explicitly checked
- **VERDICT_CONFORMANCE**: implementation checked against the evaluation verdict
- **ARTIFACT_WRITTEN**: findings file exists (REVIEWED or N/A)
