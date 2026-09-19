# Hardware Verification — Atom Lite + DTU + sensors

**Node name**: `sprout`.
**Verified**: 2026-09-12 (on the dev machine, `/dev/ttyUSB0`)
**Updated**: 2026-09-16 — Watering Unit connected & fully verified (moisture ADC on
G32, pump enable on G26 via Grove; supervised 5 s fail-off motor test ×3); Atom
stacked on DTU base (9-pin), ENV III on base Port A (G21/G25); **final combined
functional pass = PASS** (moisture → pump 5 s fail-off → moisture unchanged →
ENV III 27.04 °C / 53.89 % RH → DTU alive).
**Method**: safe, read-only probing — no DTU configuration written, no esptool
flashing (except the documented MicroPython install and full factory-backup
read); the only active pin drives were the user-supervised pump fail-off tests
(§5).
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
| **PCA9548A I2C mux** | Was `0x70` on the 2026-09-12 rig (Grove bus); **not in the current 9-pin stacked topology** (Grove bus now carries only the Watering Unit, which is not I2C) | superseded 2026-09-16 |
| **SHT30** temp/humidity | **On DTU base Port A bus (SCL=G21, SDA=G25)**, direct `0x44`; measured **25.41 °C / 56.03 % RH** (2026-09-16) | ✅ working |
| **QMP6988** pressure | On Port A bus direct `0x70`; **chip ID reads clean `0x5C`** (2026-09-16) — no mux in line, collision resolved | ✅ address fixed; pressure values pending Phase 2 driver |
| Soil-moisture sensor (Watering Unit probe) | **Analog on GPIO32** (Grove "SCL") — measured air 2068 / water ~1580 (12-bit) | ✅ connected, verified 2026-09-16 |
| M5Stack **Watering Unit (U101)** (pump + capacitive probe) | **Pump on GPIO26** (Grove "SDA"), active-HIGH; supervised 5 s fail-off motor test passed 2× | ✅ connected, verified 2026-09-16 |
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
| DTU base **Port A** (ENV III) | **SCL=G21, SDA=G25** (separate I2C bus from the Atom Grove) | blueprint n/a | SHT30@`0x44` + QMP6988@`0x70`, no mux; watering unit on G26/G32 does NOT collide |
| Watering Unit moisture (analog) | **GPIO32 (Grove SCL)** | ADC GPIO32 | ✅ confirmed by submersion test (air ≈2068 / water ≈1580 counts); SCL shares the Grove I2C line — conflicts with any I2C device on the same bus |
| Watering Unit pump | **GPIO26 (Grove SDA)** | GPIO26 | ✅ confirmed, active-HIGH (HIGH = ON); 5 s fail-off test spun motor reliably; pin left driven LOW; still requires hardware pull-down (§5) |
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
Two independent I2C buses in the 2026-09-16 stacked topology:
```
Atom Grove bus (SCL=G32, SDA=G26): none (I2C)   ← Watering Unit only (analog+GPIO)
DTU Port A bus  (SCL=G21, SDA=G25): 0x44, 0x70   ← ENV III: SHT30 + QMP6988 (no mux)
```
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
- **Soil moisture is analog, not I2C.** The M5Stack Watering Unit's probe is an
  **analog (capacitive) ADC** output confirmed on **GPIO32** and its pump a
  **GPIO** output on **GPIO26** (U101; moisture ADC + pump control, both via the
  Grove cable — white=moisture→G32, yellow=PUMP_EN→G26, red=5V, black=GND).
  Verified 2026-09-16: air ≈2068 / submerged ≈1580 counts (~1819/~1435 mV),
  stable and reproducible after ~60 s settling. **⚠️ Both pins are on the Grove
  I2C bus (G32=SCL, G26=SDA) — the moisture analog output and pump drive share
  the I2C lines. This is fine while only the Watering Unit is on the Grove
  port, but ANY I2C device added to that bus (e.g. an ENV III via the DTU base
  Grove port) will collide with the moisture signal on SCL and the pump drive
  on SDA. Plan a non-I2C port for the Watering Unit before adding I2C sensors.**

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
4. **Supervised manual motor tests (2026-09-16, outside any gate):** two 5 s
   fail-off cycles were run at the user's request with the pump recirculating
   in water, both motor power rails live and the user ready to cut power. The
   script drives G26 LOW first, holds HIGH 5 s, then drives LOW again and
   leaves the pin **driven LOW** (never deinit/floats). Both runs: motor spun
   during ON, stopped at OFF, no residual running. This verifies the drive
   path, but it is **not** a substitute for the hardware pull-down + default-OFF
   `boot.py` — both are still required before the pump is trusted by firmware.
5. **Min ON / min OFF / max daily** enforcement from the control engine, hard
   overrides always winning.

### Concrete fix recipe (do this before reconnecting the pump)

The U101 is **active-HIGH**: `PUMP_EN` (yellow wire) HIGH = pump ON (M5Stack's
example does `digitalWrite(PUMP_PIN, 1)`; U101 pin map: Black=GND, Red=5V,
Yellow=PUMP_EN, White=moisture analog out). The "motor always on" happens when
PUMP_EN sits on a line that is high/floating at power-on — most commonly the
**I2C Grove port**, whose SDA/SCL are pulled up (on this rig G26/G32 already
carry the PCA9548A mux + ENV III; never put PUMP_EN there).

1. **Wire it to a non-I2C port.** Here the Atom's Grove port is the I2C bus
   (G26/G32, occupied by the mux). Use the DTU base's J1 header (G21/G25) or
   another free GPIO — one pin for PUMP_EN, one **ADC** pin for the moisture
   output. Preferred: moisture on **G33** (ADC1, works with WiFi on) or G25
   (ADC2, WiFi must be off); PUMP_EN on **G21/G23** (normal GPIOs, not
   strapping pins). Avoid ESP32 strapping pins (GPIO0/2/5/12/15) for the pump.
2. **Fit a 10 kΩ pull-down from PUMP_EN to GND**, physically at the unit's
   connector. This defines the input as LOW (OFF) whenever the GPIO is
   floating/uninitialized — including the whole boot, reset, flashing,
   deep-sleep and crash windows. This is the actual fix; firmware alone cannot
   cover the window before MicroPython starts.
3. **Firmware drives OFF first, before anything else** in `boot.py`:
   `from machine import Pin; Pin(PUMP_PIN, Pin.OUT, value=0)`. Keep this as the
   very first executable line (before WiFi/UART/I2C/deep-sleep setup) and
   re-assert OFF on every wake/deep-sleep path. Never rely on `Pin.OUT` alone
   without the external pull-down.
4. **Power the pump separately.** The pump is 5 W (~1 A at 5 V). Do not run it
   from the Atom's USB/Grove 5 V rail; use a dedicated 5 V supply with a common
   ground (or the base's 12 V input with a suitable buck, respecting its
   current limit). Keep the pump's ground star-connected to the unit ground.
5. **Verify without water before reconnecting the pump:**
   - With the pump motors unplugged, drive PUMP_EN and measure the unit's pump
     connector (or an LED + 1 kΩ across the motor terminals) — must read ~0 V
     at boot and with GPIO LOW.
   - Toggle the GPIO in a test and confirm the output goes high only when
     commanded.
   - Then connect the pump on a current-limited bench supply (set ~1.2 A,
     5 V) and repeat the boot test: pump must stay off through reset/flash
     cycles before it is ever used with water.
6. **If a future module is active-LOW:** invert step 2 (10 kΩ pull-up to 3.3 V)
   and drive HIGH first in `boot.py`. Determine polarity with the LED test
   first — never guess.

## 5a. ENV III Port A contact — issue #124 (known weak point)

**Symptom:** the ENV III (SHT30/QMP6988 on the DTU base **Port A**, G21/G25)
intermittently vanishes from the bus; every I2C op returns `OSError ETIMEDOUT`.
Dropped **4+ times on 2026-09-16**, always recovered by reseating the Grove
plug in Port A.

**Diagnosed failure signature (2026-09-16):** the Grove plug's **SCL pin
(G21) loses electrical contact** while SDA (G25) stays seated:

- `G21` with internal pull-up engaged reads `0` (floating low, no board pull-up
  present on that line) until force-driven high once, then reads `1` → **open
  contact**, not a short.
- `G25` reads `1` (board pull-up live → SDA still connected).
- Consequence: SCL held/floating low → clock dead → `ETIMEDOUT` on anything →
  the sensor reports missing.
- Partial engagement (one pin not seated) is the likely cause; vibration or
  moving the rig triggers it.

**Mitigations:**
- **Software (in this repo):** `test_sprout_e2e.py` retries ENV III reads **once**
  before FAIL (loud note; a genuine absence still fails) and `probe_hardware.py`
  emits the `g21`/`g25` pin states so a failed probe says which line is open.
- **Hardware (recommended, the actual fix):**
  1. Fully seat the Grove plug until it *clicks*; verify both latch tabs.
  2. **Strain relief** — a dot of tape/blu-tack around the plug-to-socket seam
     (or a short Grove cable to the ENV III) so movement can't lever the SCL
     pin open.
  3. Avoid moving the rig while it is powered; reseat at power-off if possible.
  4. If the socket is worn, relocate the ENV III to a spare Grove port (verify
     it is on G21/G25 or update the pin map).

---

## 5b. Native default-OFF + floating-line observation (2026-09-18)

The node now runs the **native-client** firmware
(`native-client/firmware/main/`), not the MicroPython `boot.py` this recipe was
written against. The default-OFF requirement is implemented in `pump.cpp`:

- `pump_init()` drives **G26 LOW as the very first action of `app_main`**, before
  NVS / UART / sensors / RNS, and enables the pad's internal pull-down as a weak
  extra (the pad driver is the authority; the internal ~45 kΩ is *not* a
  substitute for the 10 kΩ hardware pull-down).
- Verified on hardware 2026-09-18: the boot log's **second line** is
  `pump: pump enable G26 driven LOW (OFF), actuation DISABLED (dry run)`.
- `pump_set(true)` is additionally refused while `PUMP_ACTUATION_ENABLED` is `0`.

**Observation (user-witnessed, 2026-09-18):** during a supervised `idf.py flash`
the ESP sits in download mode with G26 floating for ~1 minute, and the pump
motor **stayed off for the whole window**. This is consistent with the topology
change in §4 — nothing pulls G26 up any more now that the I2C mux / ENV III are
off the Grove bus — but it is *one observation*, not a fix: the "always on"
failure mode in §5 was measured on the old topology, and a floating control line
remains undefined behaviour. **The 10 kΩ pull-down stays required (issue #119)
before firmware ever actuates the pump.**

---

## 6. Open items

| Item | Needed for | Owner |
|------|-----------|-------|
| ~~QMP6988 `0x70` collision~~ **RESOLVED 2026-09-16 by topology:** ENV III on DTU Port A (G21/G25) has no mux in line → chip ID `0x5C` reads clean. Still to do: real pressure readout in Phase 2 (config/sample sequence) | pressure telemetry + pressure-trend input | Phase 2 driver |
| ~~Soil-moisture pins~~ **DONE 2026-09-16:** moisture ADC on G32, calibrated air≈2068 / water≈1580; full moisture calibration curve + soil-data point still pending. **Live 2026-09-18 data:** probe-in-air ≤ 1.4 %, probe seated in the Kale pot 42.0–47.5 % → a 5 % plausibility floor safely separates "not in soil" from "dry soil" (`algorithm-evaluation.md` §1.4 A1.5) | Phase 2/3 calibration & control | done + user |
| ~~Pump pin & polarity~~ **DONE 2026-09-16:** pump enable on G26, active-HIGH, 5 s fail-off test passed. **Default-OFF is now implemented in the native firmware and hardware-verified** as the first boot action (2026-09-18, `pump.cpp`, §5b); **the 10 kΩ hardware pull-down is still to be fitted** before firmware may energise the pump | Phase 5 pump driver | firmware done, pull-down pending (#119) |
| Resolve Grove-port sharing before stacking DTU base + ENV III with the Watering Unit (both would drive/share G26/G32) | air T/humidity on same bus as moisture/pump | user + firmware |
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
