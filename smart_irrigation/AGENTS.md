# Smart Irrigation project rules

These rules are mandatory for every coding agent working under
`smart_irrigation/`. They are enforced by the `smart-irrigation-dev` Archon
workflow (`.archon/workflows/smart-irrigation-dev.yaml`) and its gate chain.

## Read first

1. `smart_irrigation/docs/development-plan.md` — the blueprint (also at
   `/home/pondycrane/smart-irrigation-dev-plan.md`).
2. `smart_irrigation/docs/algorithm-evaluation.md` — mandatory deliverable.
3. `/home/pondycrane/LMAO/AGENTS.md` — repo-wide LMAO rules (they all apply).

## Algorithm evaluation mandate (blueprint Appendix 13)

**Do not write a single line of firmware until
`docs/algorithm-evaluation.md` exists** (and has been reviewed/merged).
The evaluation must contain, with benchmark evidence:

1. **Algorithm verdict** — state machine w/ hysteresis vs PID vs fuzzy vs
   model-based/hybrid, compared on accuracy, memory, calibration effort,
   robustness, ML-trainability. Justify the winner.
2. **Rulebase / membership functions** — if fuzzy/hybrid: full compressed
   (5×4×3) rule table, optimized membership parameters, response surface,
   pressure-trend sensitivity analysis (drop the input if it changes output
   < 10% in any case).
3. **ET₀ model** — if model-based/hybrid: simplified Hargreaves/Penman-Monteith
   using T + humidity only, calibrated coefficient, Python reference impl.
4. **ML dataset schema** — the batched 30-min training format
   (`batch_encoder.py` reference) and the on-node features to collect.
5. **Protocol recommendation** — full µReticulum/LXMF on-node vs minimal UART
   binary to the DTU, with firmware size estimates for each.
6. **Risk assessment** — soil-type mismatch, pressure noise, pump dynamics,
   I2C lockup, battery/solar, duty cycle.
7. **Recommended development plan** — an optimized phase plan derived from the
   evaluation, with any blueprint corrections (e.g. pin/board assumptions).

The firmware architecture follows the evaluation verdict. Do not silently
implement the blueprint's sketch (e.g. its fuzzy engine) if the evaluation
selected something else — implement the verdict, with the sketch as fallback.

## Hardware safety (violating these can brick devices)

- **NEVER use esptool** for probing, `chip_id`, or flashing on the Cardputer
  (`/dev/ttyACM*`), the RNode (`/dev/ttyUSB*`), or the Atom Lite. The Cardputer
  is flashed only via `bazel run //cardputer_client:flash` (raw REPL); the RNode
  only via https://flasher.rnode.network/.
- Atom Lite file uploads go **only** through the project flash tool (MicroPython
  raw REPL, adapted from `cardputer_client/flash.py`). The one-time MicroPython
  firmware install is a separate documented procedure requiring explicit user
  confirmation — it is not part of any automated gate.
- Never open ad-hoc serial sessions (`screen`, `minicom`, REPL experiments) on
  a device while a gate or E2E test is running.
- **Leave the production Cardputer running** with its production config
  (`DEST_HASH`, server radio params) after any test session.
- Radio parameters (868 MHz, BW 125 kHz, SF 7, CR 4:5, preamble 24, syncword
  0x1424) stay in sync between server and node unless the task requires a
  change; LoRaWAN region (EU868/US915/AS923) is a task-level decision.

## Firmware constraints

- **MicroPython only** on the Atom Lite. No C/C++/ESP-IDF there; C is only on
  the STM32WLE5CC side (PlatformIO).
- **No `float` if avoidable** — fixed-point Q8.8 for the control engine.
  Memory budget: control engine ≤ 30 KB RAM.
- **I2C at 100 kHz** (PCA9548A + long sensor wires), never 400 kHz by default.
- **Deep sleep between cycles is mandatory**; keep the watchdog + heap
  fragmentation recovery patterns from `cardputer_client` (issues #71/#74).
- **Sensor IDs** follow blueprint §3.1: 1 die temp, 2 humidity, 3 air temp,
  4 soil moisture, 5 pressure, 6 pump duration, 7 pump active, 8 battery,
  9 RSSI. The `SensorReport`/`CommandRequest`/`CommandAck` protobuf wire
  format must stay compatible with `cardputer_client/proto/lma_encoder.py`
  and the server's LXMF handler.
- **Pump safety is hard-coded, never fuzzy**: saturated-soil lockout,
  pressure-fall override (e.g. < −2 hPa/h → pump off), battery cutoff
  (< 3.0 V → no watering), pump min ON / min OFF times, max daily watering.
  Safety overrides must fail off (pump de-energized) on any error.
- Node config mirrors `cardputer_client/config.py` structure.

## Build & test rules (LMAO repo-wide rules apply)

- **Bazel is canonical.** Every new `*.py` under `smart_irrigation/` must
  belong to a Bazel target:
  - host-testable logic → `py_library`/`py_test` (mirror `cardputer_client/BUILD`
    and `tests/BUILD` patterns, including `conftest.py` in `srcs` and
    `imports = ["."]` where siblings use them),
  - MicroPython entry files (`main.py`, `boot.py`, `config.py`, `flash.py`) →
    `exports_files`/`filegroup` (they are transferred to the device, not run on
    the host),
  - vendored `firmware/lib/urns/**` → globbed `filegroup` only; never lint,
    "fix", or add per-file targets.
- Host unit tests run on CPython with MicroPython mocks (the
  `tests/test_urns_*.py` pattern). The control engine must have deterministic
  unit tests plus a sweep regression (monotonicity: drier soil ⇒ ≥ pump time;
  safety overrides always win).
- Lint/type: `ruff check`, `ruff format --check`, `mypy` on changed files only.
- Unit gate: `bazel test //tests:all --test_tag_filters=-requires_hardware`.
- E2E gate: hardware tests as scheduled by `irrigation-hardware-e2e`; loud
  skip/UNVERIFIABLE is recorded in the PR — never a silent pass.

## Blueprint working assumptions (validate against hardware, don't guess)

The blueprint contains known unknowns (see its §9): moisture sensor type
(analog vs I2C), PCA9548A channel map, LoRaWAN region, pump drive type, UART
cross-wiring, battery divider ratio, and the Atom Lite variant/SoC (blueprint
says ESP32-S2; the classic Atom Lite is ESP32). Treat pin/board details as
working assumptions to verify with `i2c_scan()` and the actual attached device;
record findings in `docs/pin-mapping.md`. Never guess a pin and never probe
hardware that is not attached.

## Artifacts

Workflow artifacts live under `$ARTIFACTS_DIR` (outside the worktree):
`assessment.md/json`, `algorithm-evaluation` output, `plan.md`,
`plan-context.md`, `plan-confirmation.md`, `implementation.md`,
`validation.md`, `hardware-e2e.md`, `production-health.md`, `review/*.md`.
Never commit artifacts into the repo.
