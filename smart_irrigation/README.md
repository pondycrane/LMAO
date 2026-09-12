# Smart Irrigation — LMAO Irrigation Node

Edge irrigation controller: fuses soil moisture + air temperature/humidity +
pressure trend into an irrigation decision, drives a pump, and reports every
sensor sample to the LMAO server over LoRa for offline ML training.

**The development blueprint is the single source of truth:**
[`docs/development-plan.md`](docs/development-plan.md)
(also at `/home/pondycrane/smart-irrigation-dev-plan.md`).
**Verified hardware reality (pin map, DTU firmware, sensor placement, known
collisions):** [`docs/hardware-verification.md`](docs/hardware-verification.md).

## System at a glance

```
Atom Lite (MicroPython)                    STM32WLE5CC DTU (LoRaWAN)
├── sensors (PCA9548A I2C hub)             ├── OTAA Class A join
│   ├── ch0 capacitive soil moisture       ├── UART bridge to Atom Lite
│   ├── ch1 SHT30 (air T / humidity)       └── LoRa uplink/downlink
│   └── ch2 QMP6988 (pressure)                        │
├── control engine (fuzzy / ET₀ model —   ▼
│   see docs/algorithm-evaluation.md)     LMAO server (K8s / Turing Pi 2)
├── pump relay/MOSFET                     ├── LXMF router → SensorReport
└── LXMF SensorReport ──── UART ──────────┤── NATS JetStream → DuckDB
                                          └── downlink CommandRequest → pump
```

## Repository layout (grown by the workflow, not scaffolded up front)

```
smart_irrigation/
├── README.md                     # this file
├── AGENTS.md                     # hard rules for coding agents
├── docs/
│   ├── development-plan.md       # the blueprint (phases 1–8 + appendices)
│   └── algorithm-evaluation.md   # MANDATORY deliverable before firmware work
├── firmware/                     # Atom Lite MicroPython node (µReticulum port)
│   ├── config.py, lora_boards.py, flash.py, boot.py, main.py
│   ├── lib/urns/                 # vendored copy of cardputer_client/lib/urns
│   ├── lib/sensors/              # pca9548a.py, dht20.py, ...
│   ├── proto/lma_encoder.py      # vendored copy
│   ├── src/                      # control engine, sensor fusion, pump
│   └── tests/                    # host-side pytest with MicroPython mocks
├── stm32-dtu/                    # STM32WLE5CC LoRaWAN bridge (C, PlatformIO)
├── k8s-app/                      # server-side ingest extensions (if not in repo root)
├── scripts/                      # sim/benchmark/dataset tools (dev machine)
└── tests/                        # E2E + integration tests (Bazel targets)
```

## Hardware (fixed — per blueprint §13, Appendix B)

**Verified on 2026-09-12** — see [`docs/hardware-verification.md`](docs/hardware-verification.md)
for evidence, pin map, and open items. Summary:

| Component | Verified reality |
|-----------|------------------|
| Atom Lite (ESP32-PICO-D4, not S2) | FTDI `0403:6001` "M5stack"; MicroPython v1.29.0; MAC `c8:85:41:67:dd:34` |
| Atom ↔ DTU UART | **TX=G22, RX=G19** @115200 (blueprint's 17/16 is wrong) |
| I2C bus | **SCL=G32, SDA=G26**; PCA9548A mux at `0x70` (blueprint's 21/22 is wrong) |
| Sensors | SHT30 `0x44` + QMP6988 on **mux ch5** (ENV III board); moisture **analog** (Watering Unit probe) |
| QMP6988 pressure | ⚠️ **address collision with the mux (`0x70` → `0x70`)** — unreadable until the hardware fix; not a control input by default |
| STM32WLE5CC DTU | M5Stack A152-EU868 / **RAK3172 RUI_4.0.6**, currently **P2P mode** (`AT+NWM=0`); AT console verified |
| Pump | M5Stack Watering Unit (U101): analog moisture + GPIO pump; pump runs whenever the control line floats |

## The algorithm evaluation gate

The blueprint's Challenge Brief (Appendix 13) **forbids writing firmware
before an algorithm evaluation exists**. `docs/algorithm-evaluation.md` must
contain: the verdict across state-machine / PID / fuzzy / model-based-hybrid,
the rulebase or ET₀ model, the ML dataset schema, the on-node protocol
recommendation, risk assessment, and a revised phase plan — with benchmark
evidence. Run it first:

```bash
cd /home/pondycrane/LMAO
archon workflow run smart-irrigation-dev "Phase 0: algorithm evaluation"
```

Then develop phases/features one at a time:

```bash
archon workflow run smart-irrigation-dev "Phase 2: PCA9548A driver + sensor drivers"
archon workflow run smart-irrigation-dev "Phase 3: control engine per evaluation verdict"
archon workflow run smart-irrigation-dev "Phase 6: UART bridge + LoRaWAN integration"
```

See [`../docs/archon-workflows.md`](../docs/archon-workflows.md) for the full
workflow reference.

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
