# ESP32 RNode Firmware — Flashing Guide

This guide covers flashing an ESP32-based RNode to act as a transparent
LoRa bridge for the LMAO Server (Raspberry Pi).

## What is an RNode?

An [RNode](https://github.com/markqvist/RNode_Firmware) is an open-source
LoRa radio that connects to a host computer via USB serial and provides a
transparent interface for the [Reticulum](https://reticulum.network/)
networking stack.

For this POC:
- **ESP32** runs RNode firmware (acts as LoRa modem)
- **Raspberry Pi** connects to ESP32 via USB and runs Reticulum + LXMF
- Reticulum's `RNodeInterface` automatically discovers and configures the RNode

---

## Hardware Required

| Item | Notes |
|------|-------|
| ESP32 dev board | e.g., ESP32-WROOM, Heltec LoRa 32, TTGO LoRa |
| LoRa radio module | SX1276 or SX1262 (often integrated on Heltec/TTGO) |
| USB cable | Data-capable (not charge-only) |
| Raspberry Pi | Any model with USB port |

> **Recommended**: Heltec WiFi LoRa 32 (V2 or V3) — has both ESP32 + SX1262
> on a single board with USB-C and OLED display.

---

## Step 1: Install `rnodeconf`

On your Raspberry Pi or development machine:

```bash
# Install via pip
pip3 install rnodeconf

# Or install from source
git clone https://github.com/markqvist/RNode_Firmware.git
cd RNode_Firmware
pip3 install .
```

Verify installation:

```bash
rnodeconf --version
```

---

## Step 2: Connect the ESP32

1. Connect the ESP32 to your machine via USB
2. Identify the serial port:

```bash
# Linux
ls /dev/ttyUSB* /dev/ttyACM*

# macOS
ls /dev/cu.usbserial*

# Usually /dev/ttyUSB0 or /dev/ttyACM0 on RPi
```

---

## Step 3: Flash RNode Firmware

### Option A: Auto-install (recommended)

`rnodeconf` can automatically detect and flash compatible boards:

```bash
# Auto-detect and flash
rnodeconf --autoinstall

# Or specify the port explicitly
rnodeconf --port /dev/ttyUSB0 --autoinstall
```

### Option B: Manual flash

```bash
# 1. Download the latest RNode firmware
wget https://github.com/markqvist/RNode_Firmware/releases/latest

# 2. Flash using rnodeconf
rnodeconf --port /dev/ttyUSB0 --firmware ./rnode_firmware_esp32.zip

# 3. Verify
rnodeconf --port /dev/ttyUSB0 --info
```

### Option C: Flash with esptool directly

```bash
# Install esptool
pip3 install esptool

# Erase flash
esptool.py --port /dev/ttyUSB0 erase_flash

# Write firmware (example for Heltec LoRa 32 V3)
esptool.py --port /dev/ttyUSB0 --baud 921600 write_flash 0x0 rnode_firmware.bin
```

---

## Step 4: Verify the RNode

After flashing, the ESP32 should appear as a serial device:

```bash
rnodeconf --port /dev/ttyUSB0 --info
```

Expected output:

```
RNode Firmware v1.55
Device: ESP32
LoRa: SX1262
Frequency: 868.0 MHz
Bandwidth: 125 kHz
SF: 7
CR: 5
TX Power: 17 dBm
Status: Ready
```

---

## Step 5: Test with Reticulum

Create a minimal Reticulum config (`~/.reticulum/config`) or use the
`lmao_server/config.py` from this repository:

```bash
# Start Reticulum with the LMAO config
cd lmao_server
python3 -c "
import config
from RNS import Reticulum
configdir = config.get_configdir()
r = Reticulum(configdir=configdir)
print('Reticulum started — RNode interface should be active')
import shutil
shutil.rmtree(configdir, ignore_errors=True)
"
```

If successful, you'll see log output showing the RNode interface coming online.

---

## Step 6: Verify E2E Test Readiness

Before running the full E2E LoRa test, confirm the Heltec RNode is
properly detected and configured:

```bash
# Verify RNode is detectable and configured for 868 MHz
rnodeconf --port /dev/ttyUSB0 --info
```

Expected output should show:
- Frequency: 868.0 MHz
- Bandwidth: 125 kHz
- SF: 7
- CR: 5

Once confirmed, connect both the Heltec RNode and the Cardputer ADV
(via USB) and run the E2E LoRa verification test:

```bash
# Run the LoRa E2E communication test
bazel test //tests:test_cardputer_lora_e2e --test_output=all
```

The test will:
1. Probe for both the RNode and Cardputer
2. Flash the Cardputer with the client code + server identity
3. Start a temporary LMAO server on the host
4. Verify bidirectional LoRa message delivery

If hardware is not detected, the test skips gracefully with a clear message.


## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `Permission denied` on `/dev/ttyUSB0` | User not in `dialout` group | `sudo usermod -a -G dialout $USER` then logout/login |
| RNode not detected | Wrong port or baud rate | Check `dmesg | tail -20` after plugging in USB |
| "No LoRa radio found" | Wrong pin mapping for your board | Use `rnodeconf --autoinstall` for auto-detection |
| ESP32 in download loop | GPIO0 pulled low | Disconnect GPIO0 from GND after flashing |
| Frequency mismatch | EU vs US band | Use 868 MHz for EU, 915 MHz for US in config |

---

## Pin Reference for Common Boards

### Heltec WiFi LoRa 32 V3

| Signal | GPIO |
|--------|------|
| NSS/CS | 8 |
| SCK | 9 |
| MOSI | 10 |
| MISO | 11 |
| RST | 12 |
| BUSY | 13 |
| DIO1 | 14 |

### TTGO LoRa32 V2.1

| Signal | GPIO |
|--------|------|
| NSS/CS | 18 |
| SCK | 5 |
| MOSI | 27 |
| MISO | 19 |
| RST | 23 |
| DIO0 | 26 |

---

## References

- [RNode Firmware GitHub](https://github.com/markqvist/RNode_Firmware)
- [Reticulum RNode Interface Docs](https://reticulum.network/manual/interfaces.html#rnode-interface)
- [rnodeconf Documentation](https://github.com/markqvist/rnodeconf)
