# Smart Irrigation — LMAO Irrigation Node

Edge irrigation controller: fuses soil moisture + air temperature/humidity +
pressure trend into an irrigation decision, drives a pump, and reports every
sensor sample to the LMAO server over LoRa for offline ML training.

**The development blueprint is the single source of truth:**
[`docs/development-plan.md`](docs/development-plan.md)
(also at `/home/pondycrane/smart-irrigation-dev-plan.md`).

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

| Component | Role |
|-----------|------|
| Atom Lite (ESP32) | MicroPython host: sensors, control engine, LXMF, pump |
| STM32WLE5CC DTU | LoRaWAN Class A radio bridge over UART |
| PCA9548A | I2C hub: ch0 moisture, ch1 SHT30, ch2 QMP6988 |
| Pump relay/MOSFET | Irrigation actuator (min on/off, max daily caps) |

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

- Never use esptool to probe/flash any device (Cardputer, RNode, Atom Lite).
  Atom Lite file uploads go only through the project flash tool (raw REPL);
  the one-time MicroPython firmware install is a separate, documented,
  user-confirmed procedure.
- Never poke serial ports ad hoc while a gate/test is running.
- Leave the production Cardputer running with its production config.
- The pump must always be driven through the safety overrides (saturation
  lockout, pressure-fall override, battery cutoff, min on/off, daily cap).
