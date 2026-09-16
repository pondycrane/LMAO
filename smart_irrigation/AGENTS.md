# Smart Irrigation project rules

These rules are mandatory for every coding agent working under
`smart_irrigation/`. They are enforced by the `smart-irrigation-dev` Archon
workflow (`.archon/workflows/smart-irrigation-dev.yaml`) and its gate chain.

## Read first

1. `smart_irrigation/docs/development-plan.md` — the blueprint (also at
   `/home/pondycrane/smart-irrigation-dev-plan.md`).
2. `smart_irrigation/docs/hardware-verification.md` — **verified hardware
   reality** (pin map, DTU firmware/mode, I2C mux + sensor placement, the
   QMP6988 address collision, watering-unit safety). Supersedes the blueprint
   wherever they conflict.
3. `smart_irrigation/docs/algorithm-evaluation.md` — mandatory deliverable.
4. `/home/pondycrane/LMAO/AGENTS.md` — repo-wide LMAO rules (they all apply).

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

- **NEVER use esptool** on the Cardputer (`/dev/ttyACM*`) or the RNode
  (`/dev/ttyUSB*`) — any probing/flashing. The Cardputer is flashed only via
  `bazel run //cardputer_client:flash` (raw REPL); the RNode only via
  https://flasher.rnode.network/.
- **Atom Lite (FTDI bridge) exception:** esptool is allowed **only** for the
  documented one-time MicroPython firmware install and a full flash backup,
  at **115200 baud** (460800+ is unreliable on the ESP32-PICO-D4 embedded
  flash). Routine file uploads use MicroPython raw REPL / `mpremote` — never
  esptool. Never use esptool as a general-purpose probe during a gate.
- Never open ad-hoc serial sessions (`screen`, `minicom`, REPL experiments) on
  a device while a gate or E2E test is running. The sanctioned probe is
  `smart_irrigation/firmware/tools/probe_hardware.py` (read-only; never touches
  pump pins; restores the mux to all-channels-off).
- **Never power the pump during probing/validation.** The watering module
  currently runs its motor whenever it is plugged in (floating control line);
  keep it disconnected until the default-OFF requirement in
  `docs/hardware-verification.md` §5 is implemented and verified.
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
  pressure-fall override (only when pressure is actually readable — see the
  QMP collision), battery cutoff (< 3.0 V → no watering), pump min ON / min OFF
  times, max daily watering. Safety overrides must fail off (pump
  de-energized) on any error, including watchdog resets and deep sleep. The
  pump control pin is configured OFF as the first action of `boot.py`.
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

## Verified hardware reality (see docs/hardware-verification.md)

**Node name: `sprout`.** Verified 2026-09-12, re-verified 2026-09-16 — use
these values, not the blueprint's guesses:

- **Atom Lite** = ESP32-PICO-D4 (not ESP32-S2), USB `0403:6001` "M5stack",
  MAC `c8:85:41:67:dd:34`, MicroPython v1.29.0 installed; 2 MB FS.
- **Stack**: Atom Lite on the **DTU LoRaWAN base (A152-EU868)** via the 9-pin
  socket; **ENV III on the base's Grove Port A**. Two independent I2C rails.
- **Atom ↔ DTU UART: TX=G22, RX=G19 @115200** (blueprint's 17/16 is wrong).
  DTU = RAK3172, **RUI_4.0.6_RAK3172-E**, currently **P2P mode (`AT+NWM=0`)**;
  LoRaWAN-only commands return `AT_MODE_NO_SUPPORT` until Phase 6 switches it.
- **Grove bus (SCL=G32, SDA=G26)** carries the **Watering Unit only** (not
  I2C): **moisture analog → G32** (air ≈2067 / submerged ≈1580, 12-bit ADC),
  **pump → G26 active-HIGH**. Confirmed by submersion test + 3× supervised 5 s
  fail-off motor tests (pump left driven LOW after each).
- **DTU base Port A bus (SCL=G21, SDA=G25)** carries the **ENV III** direct
  (no mux): **SHT30 @0x44** (air T/humidity — 27.04 °C / 53.89 % RH) and
  **QMP6988 @0x70**. **The PCA9548A is no longer in the path**; the old QMP
  `0x70` collision is **resolved** — chip ID reads clean `0x5C`. Pressure hPa
  readout still needs the Phase 2 QMP config/sample driver.
- **Watering Unit U101 pump safety is still mandatory**: pump control must be
  driven OFF as the first action in `boot.py` and a **hardware pull-down must
  be fitted** before firmware ever drives the pump; no pump actuation in
  gates/probes. Supervised manual tests (2026-09-16) verified the drive path
  but do not substitute for pull-down + default-OFF `boot.py` (issue #119).
- Known weak point: the ENV III Grove plug on Port A is contact-sensitive — a
  loose connection makes the SHT30/QMP vanish from the bus (reseat to fix).
- Blueprint remaining unknowns: LoRaWAN region plan, battery divider (if any).
  Never guess a pin; record new findings in `docs/hardware-verification.md`.

## Artifacts

Workflow artifacts live under `$ARTIFACTS_DIR` (outside the worktree):
`assessment.md/json`, `algorithm-evaluation` output, `plan.md`,
`plan-context.md`, `plan-confirmation.md`, `implementation.md`,
`validation.md`, `hardware-e2e.md`, `production-health.md`, `review/*.md`.
Never commit artifacts into the repo.
