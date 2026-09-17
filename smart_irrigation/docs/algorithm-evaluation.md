# Sprout Algorithm Evaluation (Phase 0 / blueprint Appendix 13)

**Node**: `sprout` — M5Stack Atom Lite (ESP32-PICO-D4) + Atom DTU LoRaWAN base (RAK3172, P2P) + Watering Unit U101 (moisture ADC + pump) + ENV III (SHT30 + QMP6988 on Port A).
**Status**: Mandatory pre-firmware deliverable (issue #109). Written as research only — no firmware code.
**Date**: 2026-09-17.

This document fulfils the 7 deliverables mandated by `docs/development-plan.md` Appendix 13 and `smart_irrigation/AGENTS.md`. It supersedes the blueprint's algorithm sketch wherever the **verified hardware reality** (`docs/hardware-verification.md`) or **real benchmark data** (this doc) conflict.

---

## 0. Evidence base (all measurements real, provenance noted)

| # | Evidence | Source | When |
|---|---|---|---|
| A | Air temp 25.32 / 25.41 °C, RH 55.68 / 56.03 % (SHT30, direct 0x44 on Port A G21/G25) | `hardware-verification.md` §1/§4 | 2026-09-16 |
| B | QMP6988 chip ID clean `0x5C` on Port A (no mux collision); **pressure readout NOT yet implemented** | `hardware-verification.md` §1/§4/§6 | 2026-09-16 |
| C | Soil moisture analog G32: air ≈ **2068**, submerged ≈ **1580** (12-bit), stable after ~60 s settle | `hardware-verification.md` §2/§4 | 2026-09-16 |
| D | Pump G26 active-HIGH, 5 W (~1 A @5 V); supervised 5 s fail-off ×2 passed; hardware pull-down + default-OFF boot still pending (firmware default-OFF shipped in native-client `main.cpp`) | `hardware-verification.md` §5; issue #119 | 2026-09-16/17 |
| E | **Live runtime T/RH series** — node `e824ad2d…`, 100 paired samples, 17:16→20:57 UTC 2026-09-17, ~60 s cadence: T 25.52–27.49 °C (dT 1.97, mean 26.37), RH 48.09–59.72 % (dH 11.6, mean 53.5); sample-to-sample noise T mean 0.038 °C (max 0.28), RH mean 0.22 (max 2.34) | DuckDB `sensor_readings` (live production stream via iot-query) | 2026-09-17 |
| F | Native-client C++ firmware (RTReticulum + DTU UART-AT P2P) built, E2E-verified (#133), sending LXMF SensorReport (sensor_id 3=air temp °C, 2=humidity %) every ~60 s over LoRa, persisted to DuckDB | issue #133/#138; live pod logs | 2026-09-17 |

---

## 1. Algorithm verdict

### 1.1 Decision

**v1: hardened two-state controller on soil moisture — a state machine with hysteresis (DRY → pump for a calibrated duration → OFF), plus the always-last hard-safety override layer.**
**v2 (post-data): the same state machine, with pump duration scaled by an ET₀/VPD factor fitted from ≥1 week of on-node data.**

The blueprint's fuzzy default is **rejected**; full model-based (ET₀ prediction) is **deferred** to v2; PID on soil moisture is **rejected**.

### 1.2 Why (benchmarked against the four Appendix-13 candidates)

**Control problem actually present:** one actuator (pump), one meaningful controlled variable (soil moisture θ), decision cadence ≥ 5 min, moisture sensor with a **coarse usable span (~488 LSB, evidence C)** and no ground-truth VWC, no flow metering, no soil-type data, no pyranometer, no rain sensor.

| Criterion | State machine + hysteresis (**chosen**) | Fuzzy (Sugeno) | PID on θ | ET₀ model-based / hybrid |
|---|---|---|---|---|
| Decision accuracy | High — the surface is monotonic "dry → water, wet → off" | Same surface, same decision — fuzzy adds no decision it can't express | Poor — large plant dead-time + slow θ time-constant ⇒ integral windup; needs model | Best in principle, but gated on uncalibrated inputs |
| Calibration effort | **Lowest** — 2 moisture thresholds + 1 duration + safety caps | High — 60 rules / membership shapes need re-fit per soil type | High — gains vs sensor noise (evidence E) and 488-LSB span | Highest — and **blocked**: no solar rad., soil coeff. `k`, or flow rate calibrated (open items, `hardware-verification.md` §6) |
| Memory / CPU | ~0 (a compare + timers) | Modest (rule table) | Small | Small |
| Robustness | Best — threshold engine cannot "run away"; override layer trivially last | Good, but more tuning surface to go wrong | Fragile here (coarse+noisy analog, slow plant) | Fragile pre-calibration (wrong `k` ⇒ wrong water) |
| ML-trainability | Good — clean state labels (water/no-water) + watering-event tags are exactly the ML target | Good | Neutral | Good (model params useful) |
| Safety integration | **Best fit** — fail-off/min-caps/default-OFF are already the control branches (#113/#119) | Extra layer | Extra layer | Extra layer |

**Key evidence against fuzzy:** with a 488-LSB moisture span (evidence C) and a monotonic control surface, the sensor cannot reliably resolve the 5 moisture classes a 5×4×3 fuzzy surface presupposes; the brief itself (A.1) concedes the surface is effectively "dry = more water, wet = no water." Fuzzy adds calibration surface without decision quality.

**Key evidence against PID (established control-theory result for irrigation):** soil moisture is an integrating plant with long transport delay; 5-min decisions vs hour-scale response ⇒ the error signal is near-zero for long stretches, then spikes after irrigation — classic PID-on-moisture failure mode (windup, oscillation). Our sensor also has only 488 LSB of range (evidence C) and ~60 s settle — gain tuning would be a per-installation exercise.

**Key evidence against full model-based v1:** all its calibration preconditions are unmet today — no pyranometer (Hargreaves needs solar radiation), soil coefficient and flow rate unknown (`hardware-verification.md` §6), pressure readout unimplemented (evidence B). Building v1 on uncalibrated models trades a tunable-but-robust controller for a brittle one.

**Evidence that the ET₀ scaler is a *v2* refinement, not v1:** over the live 3.7 h benchmark window (evidence E), the ET₀/VPD proxy spans **2.63–3.81 mm/day (mean 3.20)**, i.e. the weather term moves watering by ±~19% — meaningful at field scale but *secondary* to the moisture state, and its coefficient needs real decay data to fit. Ship the robust core first; scale later.

### 1.3 v1 control law (specification)

```
every DECISION_PERIOD (300 s), after moisture ADC settles (~60 s):
  θ_raw = adc(G32)                       # verified: ~2068 air / ~1580 submerged (C)
  θ_norm = normalize(θ_raw)               # 2-point curve: counts -> 0..100 (submerged=100)

  # Hysteresis state machine (counts-space thresholds, ± band sized to reject contact noise)
  if θ_norm < DRY and pump_state == OFF and now - last_water_off >= MIN_OFF:   pump ON
  if θ_norm > WET and pump_state == ON:                                        pump OFF
  if pump_state == ON and num_seconds >= MIN_ON:                                allow turn-off
  if pump_state == ON and num_seconds >= WATER_DURATION:                        pump OFF (force)

  # Hard safety overrides — always applied AFTER the control output (never bypassed):
  if now - last_water_off < MIN_OFF or daily_pump_s >= MAX_DAILY:  force pump OFF
  if θ_norm > SATURATION (85%) or humidity > 90%:                  force pump OFF (+2 h lockout)
  pump_enable(G26) := OFF_as_first_boot_action, then per the above   # #119 default-OFF layer
```

- **Calibrated inputs:** `DRY` / `WET` thresholds (counts → normalized), `WATER_DURATION` (s), `MIN_ON = 5 s`, `MIN_OFF` (≥10 s, pump protection), `MAX_DAILY` (min), `SATURATION = 85 %`.
- **Defaults to seed from verified hardware** until soil calibration exists (flagged, see §6-R1):
  - water ≈ 1580 counts ⇒ normalize 1580→100 %, air ≈ 2068 counts ⇒ 2068→0 %.
  - `DRY = 35 %` , `WET = 60 %` (revisit after soil sample + flow calibration).
- **Decision cadence** 300 s: pump duty is slow; 60 s pushes are for telemetry (evidence E shows T/RH don't need faster control sampling).
- **Moisture noise band:** hysteresis ≥ ±30 LSB = 12 % of span (evidence C); the observed T/RH noise (evidence E) is far below any control threshold — T/RH are monitoring/ET₀ inputs only, never on-off triggers.

---

## 2. Rulebase / membership functions

Not applicable to the chosen v1 (no fuzzy engine). Direct answers to the brief's conditional asks:

- **Compressed rule table:** the chosen state machine *is* the compressed rule table — it expresses the entire monotonic control surface in 2 thresholds + 4 safety timers. A full 5×4×3 = 60-rule fuzzy surface is 30× the tuning surface for the same control decisions.
- **Pressure-trend membership (mandated sensitivity check):** **dropped from control inputs.** Grounds: (i) QMP6988 **pressure readout is not implemented** (only chip-ID verified; evidence B) — there is no pressure input path today; (ii) the brief's own hypothesis is that hourly pressure-trend is noise at the 5-min decision horizon, and the T/RH data (evidence E) shows the ET₀ input itself varies only ±19 % — pressure-trend would sit far below the 10 % decision-impact floor. Pressure (sensor_id 5) is kept **for ML telemetry only** once the readout driver lands.
- **Defuzzification:** moot (no fuzzy); if a future fuzzy layer is ever justified, **Sugeno constant-consequent** (weighted average, one division) is the correct on-node choice — noted here so it is not re-derived.
- **Safety constraints (carried from the brief, now first-class):** saturation lockout (θ>85 % ∧ RH>90 % → skip 2 h), min-interval, pump min-ON/min-OFF, max-daily caps, plus the #119 default-OFF boot ordering. These are **always applied after** the state-machine output.

---

## 3. ET₀ model (v2 scaler)

No pyranometer ⇒ use a **VPD-based transpiration proxy** (bounded soil physics; the standard simplification when solar radiation is absent).

```
es(T)  = 0.6108 · exp(17.27·T / (T + 237.3))            # saturation vapour pressure, kPa
VPD    = es(T) · (1 − RH/100)                            # vapour-pressure deficit, kPa
ET0est = k · VPD                                         # k fitted from on-node moisture decay
WATER_SCALE = clamp(ET0est / ET0_REF, 0.5, 1.5)          # multiplies WATER_DURATION; == 1.0 until k fitted
```

**Benchmarked with live data (evidence E):**

| Condition | T/RH | VPD | ET₀est (k=2.0) |
|---|---|---|---|
| benchmark mean | 26.4 °C / 53.5 % | 1.60 kPa | 3.20 mm/day |
| max-ET₀ sample | 27.5 °C / 48.1 % | 1.91 kPa | 3.81 mm/day |
| min-ET₀ sample | 25.5 °C / 59.7 % | 1.32 kPa | 2.63 mm/day |

→ The weather term moves watering by ±~19 % within one afternoon indoors. **v1 ignores it; v2 multiplies duration by `WATER_SCALE` after ≥1 week of decay data fixes `k`** (least-squares k from logged `Δθ` vs `VPD·Δt`; a flowchart/colocation note is in §7). Hargreaves-Samani itself is **not** usable on-node (needs solar radiation + a daily ΔT term; our SHT30 gives neither), so VPD is the chosen surrogate and is documented as such.

---

## 4. ML dataset schema (long-term)

**Folded to the shipped architecture:** the node is on µReticulum/LXMF + P2P DTU (evidence F), so **batching happens server-side** (`batch_encoder`, Phase 8 / #118 carry-over) from the DuckDB stream — not on-node as the blueprint's LoRaWAN batching assumed. The schema is unchanged; only the assembly point moved.

Per-sample telemetry (sensor_id already live/planned, blueprint §3.1 + native-client):

| sensor_id | meaning | status |
|---|---|---|
| 2 | humidity % (SHT30) | **live** |
| 3 | air temp °C (SHT30) | **live** |
| 4 | soil moisture % (calibrated) | to add (v1 controller) |
| 6 / 7 | pump duration (s) / pump active (0/1) | to add (v1 controller; **required for watering-event tagging**) |
| 5 | pressure hPa (QMP6988) | to add (ML-only; §2) |
| 8 / 9 | battery V / RSSI dBm | later |

Derived features (server-side, from the 60 s raw stream):
- 5-min rates: `Δθ/min`, `ΔT/min`, `ΔH/min`, and `ΔP/min` (when pressure arrives).
- Event tags: `last_water_t`, `last_water_duration`, `daily_water_s`.
- **Moisture decay trajectory after each watering event** (the highest-value target — the ML learns "given X water over Y min at T/RH Z, how does θ decay over the next 24 h") → requires the controller to emit pump-active transitions (sensor_id 6/7) with the θ trace between events.

Batch row (30-min window, 6×5-min post-watering samples) — the training sample:

```json
{
  "timestamp": "2026-09-11T10:00:00Z",
  "soil_type": "pending_user_input",
  "flow_ml_per_s": 40,
  "moisture_pre": 25.3,
  "pump_on": true,
  "pump_duration_s": 45,
  "moisture_post": [35.1, 33.8, 32.0, 30.5, 29.1, 27.9],
  "temp_trajectory": [32.1, 31.8, 31.2, 30.7, 30.2, 29.8],
  "humidity_trajectory": [45, 47, 50, 53, 55, 58],
  "pressure_trajectory": [1012, 1011, 1010, 1009, 1008, 1007]
}
```

**Prereq (dataset validity):** normalize `timestamp_ms` to wall-clock **before** training — the #127 follow-up note confirmed the stored device epoch is not real wall-clock time. `batch_encoder.py` (reference implementation) is a coding-phase deliverable (#118), not part of this research document.

---

## 5. Protocol recommendation

**Keep full µReticulum/LXMF on-node (native-client) — decided by shipped evidence, not theory.**

- The brief's minimal-UART alternative assumed "no LMAO server / no µRs" existed, sized ~40–60 KB vs ~200 KB for full µRs. **Both assumptions are stale**: the µRs/LXMF-on-node firmware is already built, E2E-verified (#133), and running in production — the server RNode receives Sprout LXMF every ~60 s and it lands in DuckDB (evidence F). The node already talks the same LXMF/allow-list/identity language as Cardputer; the allow-list gate (#132) and identity persistence live in that path.
- Reverting to raw UART blobs would require new server-side framing/decoding, lose the sender-identity → allow-list security, and lose ACK/reliability — to save flash bytes that no longer gate anything (the current firmware builds and fits).
- Firmware-size estimate: the brief's order-of-magnitude (~200 KB full µRs) is the right ballpark for the vendored RTReticulum + mbedTLS (Ed25519) + DTU-AT layer; precise out-of-the-box byte size is a build-metadata question, not a decision input. If flash ever becomes the constraint, the correct lever is trimming unused RNS features (e.g. no LINK/LINKREQUEST, no encrypted sessions beyond our single LXMF flow), **not** abandoning the protocol.

**Blueprint corrections (this deliverable also resolves the P2P-vs-LoRaWAN branch):** the radio verdict went **LoRa P2P** (AT+NWM=0, EU868) — implemented and in production (issue #116, closed). The blueprint's "node → UART → STM32 wraps LoRaWAN uplink / OTAA Class A" path is superseded; the node runs RNS over the DTU P2P link to the existing RNode/server path.

---

## 6. Risk assessment

| ID | Risk | Evidence | Mitigation (in verdict) |
|---|---|---|---|
| R1 | **Soil-type / calibration mismatch** — counts→VWC mapping wrong for the actual soil; only 488-LSB sensor span | evidence C; `hardware-verification.md` §6 ("calibration curve + soil data point pending") | 2-point verified curve (air≈2068/sub≈1580) is a floor, not the finish — one soil sample + flow calibration before trusting v1 thresholds; `soil_type` is an explicit ML field (§4) |
| R2 | **Sensor contact / baseline drift** — moisture baseline shifts with probe contact; ENV III Port A SCL contact is a documented weak point (#124) | `hardware-verification.md` §5a; issue #124 | hysteresis band (≥12 % of span); ENV III T/RH only fed to ET₀/ML, never on-off; fail-safe defaults |
| R3 | **Pump dynamics** — flow rate unknown ⇒ "water X s" ≠ "water X ml"; moisture may not rise as commanded | factual open item, `hardware-verification.md` §6 | one-time flow calibration (ml/s) recorded as an ML field (flow basis of §4 rows); v1 keeps conservative durations anyway |
| R4 | **Pump safety / always-on** — active-HIGH U101 powers on pump if the line floats | #119; `hardware-verification.md` §5 | firmware default-OFF already first boot action (native-client `main.cpp`); hardware 10 kΩ pull-down still to fit (issue #119 open); gate never actuates pump |
| R5 | **Grove-port sharing** — moisture ADC (G32) + pump (G26) sit on the Grove I2C lines; adding I2C devices there collides | `hardware-verification.md` §4 | keep Watering Unit alone on Grove; ENV III stays on DTU Port A; never add I2C to the Grove bus |
| R6 | **Pressure readout absent / 0x70 history** — no usable pressure until driver lands | evidence B | pressure excluded from control (§2); ML-only later |
| R7 | **Power / duty cycle** — pump 5 W needs a separate rail; 60 s pushes are fine at EU868 P2P but batch to 5-min for ML | `hardware-verification.md` §5.4; evidence E | separate 5 V supply (documented recipe); telemetry cadence downgrade does not affect control quality |
| R8 | **No control ground-truth** — v1 is open-loop in plant terms; no plant-water-stress feedback | architectural | the ML dataset (§4) with watering-event tags is the feedback instrument; revisit after ≥1 week |
| R9 | **ET₀ coefficient** — `k` guess wrong ⇒ v2 over/under-waters | §3 | `WATER_SCALE` clamped 0.5–1.5; fitted, not assumed; degrades to 1.0 (v1 behavior) until fitted |

---

## 7. Recommended development plan (derived from the verdict)

Replaces the blueprint's Phase 3 (fuzzy engine) and re-sequences the rest against the shipped native-client base. **No firmware until this evaluation is reviewed/merged** (mandate).

1. **P3a — v1 controller (control engine, issue #113 first slice):**
   hysteresis state machine (§1.3) in the native-client, with the always-last safety override layer (#113) and the already-shipped pump default-OFF (#119). Emit sensor_id **4** (moisture) and **6/7** (pump duration/active) for ML.
   - Host unit tests (native-client `tests/`): threshold/hysteresis transitions, override precedence (saturation lockout, min-ON/OFF, max-daily), fail-off on read error/NaN.
2. **Calibration (one-time, user-assisted, hardware):** 10 kΩ pull-down on G26 (#119); moisture soil sample (2-point + soil type); pump flow trial (ml/s). These unblock trusting v1 thresholds.
3. **P3b — ET₀ v2 scaler (post-data):** after ≥1 week of `Δθ`/VPD data, fit `k` (§3), enable `WATER_SCALE`.
4. **Downlink (issue #115):** CommandRequest (`pump_on/off`, duration update, reboot) → the same state-machine core mutates parameters, overrides still last.
5. **Pressure (sensor_id 5, ML-only):** QMP6988 readout driver; stays out of control (§2).
6. **ML/telemetry (issue #118 carry-over):** `batch_encoder.py` server-side from DuckDB with wall-clock normalization (§4 prereq).
7. **Power (issue #117):** deep-sleep + profiling + 72 h field test — sensible only after the controller exists; reuses the decision cadence to bound the sleep window.

**Blueprint corrections surfaced by the evaluation:**
- Chip is ESP32-PICO-D4 (the brief repeatedly says ESP32-S2) — native-client PICO-D4 scaffold already reflects reality.
- ENV III is **direct** on DTU Port A G21/G25 (0x44, 0x70), no PCA9548A in line (`hardware-verification.md` — the brief's mux ch5/ch1 guesses are disproven).
- Radio is **LoRa P2P**, not LoRaWAN.
- Pressure-trend is **not** a control input (§2).
- ML batched schema is assembled **server-side**, not on-node (§4).
- Protocol is **full µRs/LXMF on-node**, not minimal UART (§5).

---

## 8. Verdict summary (one paragraph)

Ship a **hysteresis state machine on soil moisture** as the v1 controller: it is the only candidate that needs no calibration inputs we don't yet have (flow rate, soil coefficient, solar radiation), fits the coarse 488-LSB moisture sensor (evidence C) and the Monotonic control surface, slots the #113/#119 hard-safety layer in as the always-last branch, and produces clean watering-event labels for the ML dataset. Add the **ET₀/VPD duration scaler as a fitted v2** once ≥1 week of real decay data exists — the live benchmark shows it would only move decisions ±~19 % today, so it cannot justify delaying the robust core. Keep **full µReticulum/LXMF on-node** (shipped, verified, required by the allow-list/identity path), batch ML server-side, drop pressure from control, and gate firmware strictly on this document's merge.
