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

## Hardware-verify required (not provable here)
1. The **SX1262 SPI response layout** — where the status byte and read data land
   relative to opcode/args in the single-transaction `transfer` — is asserted
   per the MicroPython `_cmd` semantics but **must be confirmed on the device**
   (register round-trip or logic analyser) before trusting RX/TX.
2. GPIO39/40 (JTAG MTCK/MTDO) reclaim + RF TX/RX against the production RNode.
These are the flash-verified leg (RF-step 4), not compile-verifiable.
