# Sprout — LMAO Smart Irrigation Node

**Node name: `sprout`** — an edge irrigation controller built on the M5Stack
Atom Lite stacked on the DTU LoRaWAN base (A152-EU868), fused with the M5Stack
Watering Unit (soil moisture + pump) and the ENV III sensor board (air
temp/humidity + pressure). It fuses soil moisture + air temperature/humidity +
pressure trend into an irrigation decision, drives a pump, and reports every
sensor sample to the LMAO server over LoRa for offline ML training.

![Sprout setup](../docs/images/sprout-setup.jpg)

**The development blueprint is the single source of truth:**
[`docs/development-plan.md`](docs/development-plan.md)
(also at `/home/pondycrane/smart-irrigation-dev-plan.md`).
**Verified hardware reality (pin map, DTU firmware, sensor placement, known
collisions):** [`docs/hardware-verification.md`](docs/hardware-verification.md).

## System at a glance (verified topology, 2026-09-16)

```
Atom Lite (MicroPython)                     STM32WLE5CC DTU (LoRaWAN) @UART G19/G22
├── Grove bus (SCL=G32, SDA=G26)            ├── OTAA / LoRa uplink+downlink
│   └── Watering Unit U101                  └── Grove Port A bus (SCL=G21, SDA=G25)
│       ├── moisture probe → ADC G32            └── ENV III
│       └── pump control   → GPIO G26               ├── SHT30 (air T / humidity)
├── control engine (verdict: docs/                  └── QMP6988 (pressure, no mux)
│   algorithm-evaluation.md)
└── LXMF SensorReport ─── UART ─────────────────────────┘
                                             │
                                             ▼
                       LMAO server (K8s / Turing Pi 2):
                       LXMF router → SensorReport → NATS → DuckDB
                       CommandRequest downlink → pump
```

## Repository layout (grown by the workflow, not scaffolded up front)

```
smart_irrigation/
├── README.md                     # this file
├── AGENTS.md                     # hard rules for coding agents
├── docs/
│   ├── development-plan.md       # the blueprint (phases 1–8 + appendices)
│   ├── hardware-verification.md  # verified pin map / wiring reality
│   └── algorithm-evaluation.md   # MANDATORY deliverable, §1.3–§1.4 = the control verdict
├── native-client/                # THE NODE FIRMWARE — ESP-IDF C++17 (issue #130+)
│   ├── BUILD                     # host cc_library/cc_test + build/flash sh_binary
│   ├── firmware/                 # main/*.cpp (control, pump, sensors, DTU-AT, LXMF)
│   │   ├── build.sh, flash.sh    # Docker ESP-IDF build / supervised flash
│   │   └── components/rtreticulum
│   ├── tests/                    # host C++ tests of the same code the device runs
│   └── host/                     # RTReticulum <-> Python RNS interop harness
├── firmware/                     # legacy MicroPython shell: config.py + tools/ only
│   └── tools/probe_hardware.py   # the sanctioned read-only hardware probe
└── hardware/enclosure/           # Sprout enclosure (OpenSCAD + STLs)
```

## Hardware (fixed — per blueprint §13, Appendix B)

**Verified 2026-09-12, re-verified 2026-09-16 (final functional pass = PASS).**
See [`docs/hardware-verification.md`](docs/hardware-verification.md) for
evidence, pin map, and open items. Summary:

| Component | Verified reality |
|-----------|------------------|
| Atom Lite (ESP32-PICO-D4, not S2) | FTDI `0403:6001` "M5stack"; MicroPython v1.29.0; MAC `c8:85:41:67:dd:34` |
| Stack | Atom Lite on **DTU LoRaWAN base (A152-EU868)** via 9-pin socket; **ENV III on base Port A** |
| Atom ↔ DTU UART | **TX=G22, RX=G19** @115200; DTU `AT` alive (**RUI_4.0.6**, P2P mode `AT+NWM=0`) |
| Grove bus (Atom) | **SCL=G32, SDA=G26** — carries the **Watering Unit only** (analog+GPIO, not I2C) |
| Watering Unit U101 | **moisture → ADC G32** (air ≈2067 / submerged ≈1580, 12-bit); **pump → GPIO G26**, active-HIGH; **3× supervised 5 s fail-off motor tests ✅**; pump left driven LOW |
| Port A bus (base) | **SCL=G21, SDA=G25** — independent I2C rail: **SHT30 @0x44** (27.04 °C / 53.89 % RH) + **QMP6988 @0x70** |
| QMP6988 pressure | ✅ **collision resolved by topology** (no mux on Port A) — chip ID clean `0x5C`; hPa readout pending Phase 2 driver |
| Pump safety | ⚠️ supervised tests done only; **hardware pull-down + default-OFF `boot.py` still mandatory** (issue #119) |

## The algorithm evaluation gate

> ⚠️ **Archaic-workflow status:** the `smart-irrigation-dev` Archon workflow is
> **NOT ready to run** — pending refinement in [#122](https://github.com/pondycrane/LMAO/issues/122).
> Until it's right-sized, run the phase gates manually: Phase 0 below, and the
> hardware E2E via `bazel test //tests:test_sprout_e2e --test_output=all`.

The blueprint's Challenge Brief (Appendix 13) **forbids writing firmware
before an algorithm evaluation exists**. `docs/algorithm-evaluation.md` must
contain: the verdict across state-machine / PID / fuzzy / model-based-hybrid,
the rulebase or ET₀ model, the ML dataset schema, the on-node protocol
recommendation, risk assessment, and a revised phase plan — with benchmark
evidence. Run it first (manually until the workflow is refined):

```bash
# Manual Phase 0 (workflow not ready yet, #122):
# author smart_irrigation/docs/algorithm-evaluation.md per blueprint Appendix 13
```

Then develop phases/features one at a time:

```bash
archon workflow run smart-irrigation-dev "Phase 2: PCA9548A driver + sensor drivers"
archon workflow run smart-irrigation-dev "Phase 3: control engine per evaluation verdict"
archon workflow run smart-irrigation-dev "Phase 6: UART bridge + LoRaWAN integration"
```

See [`../docs/archon-workflows.md`](../docs/archon-workflows.md) for the full
workflow reference.

## Irrigation controller (v1 — in `native-client/`)

The control law is the verdict of `docs/algorithm-evaluation.md` §1.3 as amended
by §1.4: a hysteresis state machine on the soil probe, where one watering
session is a **pulse train** — 5 s pulses, a soak of at least the 90 s/probe
settle time, the probe re-read after every soak, and an early stop as soon as
the reading reaches `WET`. A session therefore cannot out-run the pot, and the
hard safety layer is evaluated last and always wins (probe read failure →
fail-off; a settled reading at/below the 5 % plausibility floor → fail-off, since
the 0 % anchor is *air* and an unseated probe looks bone dry; saturation 85 % /
air RH 90 % → off + 2 h lockout; min ON / min OFF; daily cap; pump pin OFF as the
first boot action, #119).

The plant's optimum is a **plant profile**: `target ± band` becomes the dry/wet
thresholds, plus the pulse/soak shape and dose caps. Shipped profiles: **kale
(default)**, herbs, tomato, succulent, generic. The node stores the profile by
name in NVS (`profile`); switching it today means a reflash or an NVS write —
remote switching lands with the LXMF downlink phase (#115).

Decisions never act on a single probe reading: only samples taken with the pump
off and settled enter a 30-sample window, and the value compared against every
threshold is the **median** of that window — the live stream contains
sub-second outliers that would otherwise move water. The window is cleared on
each pump edge so a post-pulse reading is never mixed with pre-pulse soil.

| Where | What |
|---|---|
| `native-client/firmware/main/control.{h,cpp}` | profiles, pulse-train state machine, hard overrides (pure C++, integer Q8.8) |
| `native-client/firmware/main/pump.{h,cpp}` | G26 driver: default-OFF, actuation gate, on-time accounting |
| `native-client/tests/control_test.cpp` | `bazel test //smart_irrigation/native-client:sprout_control_test` |

⚠️ **Actuation stays off** — `PUMP_ACTUATION_ENABLED` is `0` in `pump.h` until the
#119 hardware pull-down is fitted and the OFF level is verified through
reset/flash. The node runs the engine in **DRY RUN**: it decides and logs, the
pump stays de-energised, sensor_id 6/7 (pump duration/active) are *not* emitted,
and nothing is written to the persisted control blob — so the ML stream and the
daily cap never count doses that did not happen.

## Cardputer chart (viewing the Sprout data)

The production Cardputer draws the Sprout soil-moisture series on its 240x135
screen: the latest value in the header, a white trace, the plant profile's
**dry** (red) and **wet** (cyan) threshold lines, and `dry/wet/n` in the footer.
It redraws on every reply, i.e. once per 300 s send cycle — matching the
Sprout's 5-minute telemetry cadence, so no cycle fetches unchanged data.

The data path adds no protocol and no airtime — the server already ACKs every
allow-listed client message, so it *piggybacks* the series on that ACK
(`cardputer_client/chart.py` parses it):

```
Sprout → sensor_id 2/3 (air humidity/temp) + 4 (moisture) + 10/11 (band) → server
server → keeps a 30-sample ring per sensor (lma_core/sprout_history.py)
       → appends "DATA <node8> <dry> <wet> <ct> <t..> <ch> <h..> <cm> <m..>" to its ACK reply
         (air series capped at 10 samples so the LXMF OPPORTUNISTIC reply stays
         under its 295-byte single-packet limit)
Cardputer → chart.draw() on each drained reply: soil (white) + humidity (green)
            on the % axis, temperature (orange) on its own right-hand °C axis
```

Notes:
- The band travels with the data, so the chart's threshold lines are the node's
  *actual* profile, not a copy that can drift, and the training stream records
  which band each sample was judged against.
- The server's ring is in memory: a server restart empties the chart, which
  refills over the next 2.5 h (30 samples x 5 min).
- The Cardputer ADV's MicroPython ships **M5.Lcd** (LovyanGFX) — there is no
  `st7789` module, so the client's old st7789 display path never engaged. The
  chart talks to a small adapter in `cardputer_client/main.py` that converts
  RGB888 to the panel's colour depth.

⚠️ **Flash the Cardputer with `bazel run //tools:install_all -- --skip-rnode`**,
not `bazel run //cardputer_client:flash`: only `install_all` injects the
server's `DEST_HASH`, and the plain flash target uploads the repo's
`config.py` (where it is `None`) — which silently stops the node sending.

## Safety (also in AGENTS.md)

- Never use esptool to probe/flash the Cardputer, RNode, or Atom Lite. Atom
  Lite file uploads go only through `mpremote`/the raw-REPL flash tool; esptool
  is allowed only for the documented one-time MicroPython install/backup at
  115200 baud.
- Never poke serial ports ad hoc while a gate/test is running. The sanctioned
  probe is `firmware/tools/probe_hardware.py` (read-only, restores the mux).
- Leave the production Cardputer running with its production config.
- **Never power the pump during probing/validation.** The Watering Unit's
  motor runs whenever its control line floats — it must stay disconnected until
  the default-OFF boot order + hardware pull-down are implemented and verified.
- The pump must always be driven through the safety overrides (saturation
  lockout, pressure override only when pressure is readable, battery cutoff,
  min on/off, daily cap), with fail-off on any error.
