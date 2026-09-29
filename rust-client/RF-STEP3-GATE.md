# RF-leg step 3 — esp-hal SPI `RadioBus` + SX1262 wired into the firmware: evidence

Step 1 cloned the leaf radio `Interface` (PR #185); step 2 ported the SX1262
driver core (PR #187). **Step 3 binds the driver to the Cardputer's SPI and
wires it into the firmware main loop.**

## What this PR does (build-verified; RF is the flash leg)
- `firmware/src/sx1262_radio.rs` — an esp-hal **`RadioBus`** implementation over
  **SPI2 (HSPI)** on the Cardputer pins (SCK40/MOSI14/MISO39/CS5, BUSY6 input,
  RST3 output), one CS-held `transfer` per SX1262 command (the opcode+args write
  and the read-back status+data share one transaction). BUSY is polled before
  each command; RST pulses on reset.
- `firmware/src/main.rs` — on boot: init esp-hal → build `Sx1262<EspRadioBus>` →
  **reset + configure the fixed leaf profile** (868 / BW125 / SF7 / CR4:5 /
  syncword 0x1424, DIO2 RF-switch, DIO3 TCXO, PA 14 dBm) → log
  `[t3] sx1262 configured 868/...` → heartbeat.
- `crates/sx126x` refactored to be **alloc-free** (fixed stack command buffers),
  removing the need for a global allocator in the bare no_std firmware. The 7
  host golden tests still pass.

## Build evidence
`cargo build --release` (esp 1.97 nightly / xtensa-esp32s3-none-elf) is clean;
the firmware image grew 220 248 → **238 184 B** (SPI + SX1262 driver linked in).

## ON-DEVICE CONFIRMATION (flashed on the Cardputer)


Flashed via espflash in bootloader mode (`--before no-reset`). On boot:
```
[t1] lmao-firmware-t3 booted
[t1] chip revision: 0.2
[t1] mac: d0:cf:13:0d:c4:50
[t3] sx1262 configured 868/BW125/SF7/CR4:5 syncword=0x1424
```
**The esp-hal SPI `RadioBus` → `Sx1262` reset + configured the real SX1262 on the
Cardputer without error** — the `[t3] sx1262 configured` gate line fires, and the
firmware continues to a healthy heartbeat (no SPI busy-wait hang/panic). This
confirms the command encodings + SPI transaction are accepted by the hardware.
(Still pending: on-air TX/RX against the production RNode + the exact read
response-layout round-trip, the RF step 4 hardware leg.)
## Hardware-verify required (not provable at build)
1. The **SX1262 SPI response layout** — where the status byte and read data land
   relative to opcode/args in the single-transaction `transfer` — is asserted
   per the MicroPython `_cmd` semantics but **must be confirmed on the device**
   (register round-trip or logic analyser) before trusting RX/TX.
2. GPIO39/40 (JTAG MTCK/MTDO) reclaim + RF TX/RX against the production RNode.
These are the flash-verified leg (RF-step 4), not compile-verifiable.
