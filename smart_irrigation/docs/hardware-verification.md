# Hardware Verification — Atom Lite + DTU + sensors

**Verified**: 2026-09-12 (on the dev machine, `/dev/ttyUSB0`)
**Method**: safe, read-only probing — no pump connected, no DTU configuration
written, no esptool flashing (except the documented MicroPython install and
full factory-backup read).
**Reproduce**: `mpremote connect /dev/ttyUSB0 run smart_irrigation/firmware/tools/probe_hardware.py`

This document is a **verified-input** for the algorithm evaluation
(`docs/algorithm-evaluation.md`), plans, and the hardware E2E gate. It
supersedes the blueprint's working assumptions (Appendix B pin map, §3.1
sensor placement) wherever they conflict.

---

## 1. Inventory (verified present)

| Device | Evidence | Status |
|--------|----------|--------|
| M5Stack **Atom Lite** (ESP32-PICO-D4) | FTDI `0403:6001` product `M5stack`, mfr `Hades2001`, serial `69526EE94F`; `esptool flash_id` → ESP32-PICO-D4 rev v1.1, 4 MB embedded flash, MAC `c8:85:41:67:dd:34` | ✅ present |
| MicroPython **v1.29.0** on the Atom | `mpremote` REPL; `os.uname()` → `esp32` / `v1.29.0 on 2026-08-24`; 2 MB MicroPython FS (2036 KB free) | ✅ installed 2026-09-12 |
| M5Stack **Atom DTU LoRaWAN-EU868** base (A152-EU868) with **RAK3172 / STM32WLE5CC** | AT console on Atom UART2 (TX=G22/RX=G19): `AT+VER=?` → `RUI_4.0.6_RAK3172-E`, `AT+HWMODEL=?` → `rak3172` | ✅ present, AT-responsive |
| **PCA9548A I2C mux** | ACKs at `0x70`; every read echoes the written byte (control register) | ✅ present |
| **SHT30** temp/humidity | `0x44` on mux **channel 5**; measured 25.32 °C / 55.68 % RH | ✅ working |
| **QMP6988** pressure | Chip ID `0x5C` detectable behind mux ch5 but **address collides with the mux (both 0x70)** | ⚠️ present, unusable via current wiring |
| Soil-moisture sensor | Not detected on I2C (no device on any mux channel) | ⚠️ not connected / analog |
| M5Stack **Watering Unit (U101)** (pump + capacitive probe) | Not connected — user-confirmed: pump runs whenever it is plugged in | ⛔ intentionally disconnected |
| Cardputer / RNode | Not attached to this machine (RNode lives on K8s tp4 per issue #93) | n/a |

**Factory firmware backup** (taken before the MicroPython install):
`/home/pondycrane/atom_lite_factory_backup_20260912.bin`
(4 MB, MD5 `ea6af1d06bc6e9db4b673bc76246ff86`).

---

## 2. Verified pin map (supersedes blueprint Appendix B)

| Function | Verified wiring | Blueprint said | Notes |
|----------|-----------------|----------------|-------|
| Atom ↔ DTU UART | **TX=G22, RX=G19**, 115200 8N1 | TX=GPIO17→PA3, RX=GPIO16←PA2 | Matches M5Stack's `M5-LoRaWAN-RAK` Atom examples (`Serial2, 19, 22`) |
| I2C (Grove port, mux bus) | **SCL=G32, SDA=G26**, 100 kHz | SDA=21, SCL=22 | Matches the M5Stack Atom Lite Grove I2C convention |
| PCA9548A mux address | **0x70** | (not specified) | Default strap; collides with QMP6988 |
| SHT30 / QMP6988 (ENV III unit) | mux **channel 5** | ch1 / ch2 | Both on the same ENV III-style board on one channel |
| Base J1 "IIC" port (G21/G25) | **empty** (no devices) | — | Not the sensor bus in this setup |
| Watering Unit moisture (analog) | not connected | ADC GPIO32 | G32 is the mux SCL — the blueprint's moisture pin conflicts with the actual I2C bus |
| Watering Unit pump | not connected | GPIO26 | G26 is the mux SDA — another blueprint conflict |
| DTU antenna / region | EU868 (A152-EU868) | EU868/US915 TBD | RAK3172 RUI4 |

---

## 3. DTU (RAK3172) findings

- Firmware: **RUI_4.0.6_RAK3172-E** (RUI4 command set; M5Stack's
  `M5-LoRaWAN-RAK` C++ library targets an older RUI generation — verify every
  AT command against the RUI4 manual before use).
- `AT+NWM=?` → **0 = LoRa P2P mode** (not LoRaWAN). LoRaWAN-only queries
  (`AT+NJM/NJS/DEVEUI/APPEUI/CLASS/DR/ADR/CFM`) return
  `AT_MODE_NO_SUPPORT` until `AT+NWM=1` is configured.
- `AT+SN=?` returns an empty value; `AT+SYSV=?` → `3.316113`.
- Read-only queries used in the probe: `AT`, `AT+VER=?`, `AT+HWMODEL=?`,
  `AT+NWM=?`. **Never** send `AT+JOIN`, `AT+SEND`, `AT+Z`, factory-reset, or
  band/config-changing commands from a probe/gate.
- For the project (blueprint Phase 6, OTAA Class A) the DTU must be
  reconfigured to LoRaWAN (`AT+NWM=1`) and provisioned with keys — that is a
  deliberate, documented step, not a probe.

---

## 4. I2C bus / sensors

```
I2C scan (SCL=G32, SDA=G26): 0x70            ← PCA9548A mux
  ch0..4, 6, 7 : empty
  ch5          : 0x44 (SHT30) + QMP6988 (0x70) ← ENV III-style board
```

- **SHT30 works**: 25.32 °C / 55.68 % RH (command `0x2C06`, 6-byte read).
- **QMP6988 address collision (BLOCKER for pressure trend):** the QMP6988 is a
  fixed-address device at `0x70` — the same address as the PCA9548A. While
  channel 5 is selected, any read at `0x70` is answered by both chips; the bus
  (open-drain) ANDs their data. The chip-ID read returns `0xD1 & 0x5C = 0x50`
  instead of `0x5C`, and all QMP register reads are corrupted the same way.
  In addition, every QMP register-address write is interpreted by the mux as a
  channel-select byte and vice versa.
  **Until fixed, barometric pressure is not a usable control input or telemetry
  field.** Fix options (pick one, hardware-level):
  1. Re-address the mux to `0x71`–`0x77` (A0–A2 straps) — preferred if the
     breakout exposes address pads.
  2. Re-address the QMP6988 to `0x71` (SDO high) — only if the board exposes SDO.
  3. Move the ENV III board off the mux (direct bus) and keep the mux for
     other devices — then the bus has SHT30 `0x44` + QMP `0x70` only.
  4. Remove the mux if only one sensor chain is needed (then no collision).
- **No soil-moisture device on I2C.** The M5Stack Watering Unit's probe is
  **analog** (ADC), and its pump is a **GPIO** output (U101, Port B style:
  moisture ADC + pump control). Confirm the actual wiring with the user before
  planning ADC/pump pins; do not reuse G26/G32 (I2C).

---

## 5. Watering Unit safety (pump "always on")

The watering module is disconnected on purpose: plugging it in powers the pump
continuously. This matches the known M5Stack Watering Unit failure mode — the
pump control line floats (or is mis-wired to a non-driving pin) and the
transistor turns on. Requirements before the pump is ever connected again:

1. **Firmware default-OFF before power:** the pump control pin must be
   configured as an output driving the OFF level as the very first action in
   `boot.py` (before any sensor/UART/LoRa init), with a hardware pull-down
   (or series resistor) so a floating/undriven line is OFF.
2. **Fail-off:** watchdog reset, exception, deep sleep, and REPL stop must all
   leave the pin OFF (external pull-down guarantees this).
3. **No pump testing in gates/probes:** `probe_hardware.py` never touches pump
   pins; the hardware E2E gate must explicitly refuse pump actuation until the
   default-OFF behavior is verified with the pump disconnected (LED/meter on
   the control line), then with the pump on a current-limited supply.
4. **Min ON / min OFF / max daily** enforcement from the control engine, hard
   overrides always winning.

---

## 6. Open items

| Item | Needed for | Owner |
|------|-----------|-------|
| Fix the QMP6988/mux address collision (one of §4's options) | pressure telemetry + any pressure-trend input | hardware/wiring |
| Confirm soil-moisture sensor model + pins (Watering Unit ADC line vs separate sensor) | Phase 2 drivers, calibration | user |
| Confirm the Watering Unit's pump control pin and OFF polarity | Phase 5 pump driver | user + firmware |
| Decide DTU mode transition (P2P currently; LoRaWAN needed for server path) | Phase 6 | evaluation/plan |
| Verify the alternative Atom↔base connectors (J1 G21/G25) | sensor relocation option | hardware |

---

## 7. Raw evidence snapshot

```
$ esptool -p /dev/ttyUSB0 -b 115200 flash_id
Chip type: ESP32-PICO-D4 (revision v1.1) ... Embedded Flash ... 4MB ... MAC: c8:85:41:67:dd:34

$ mpremote connect /dev/ttyUSB0 exec "import os; print(os.uname())"
(sysname='esp32', nodename='esp32', release='1.29.0', version='v1.29.0 on 2026-08-24', ...)

$ mpremote connect /dev/ttyUSB0 run .../probe_hardware.py
PROBE DEVICE machine=Generic ESP32 module with ESP32 freq_hz=160000000 platform=esp32 flash_bytes=4194304 mac=c8:85:41:67:dd:34 reset_cause=5 version=1.29.0
PROBE I2C scl=32 sda=26 devices=0x70
PROBE MUX addr=0x70
PROBE MUX_CH ch=5 devices=0x44 QMP_COLLISION(read=0x50)
PROBE SHT30 ch=5 addr=0x44 temp_c=25.32 humidity_pct=55.68
PROBE DTU tx=22 rx=19 baud=115200 alive=True ver=RUI_4.0.6_RAK3172-E hwmodel=rak3172 nwm=0
PROBE DONE note=pressure/QMP requires the address-collision fix
```
