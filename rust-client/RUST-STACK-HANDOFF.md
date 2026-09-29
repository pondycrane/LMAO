# Rust Stack — Handoff Status

## Host stack (complete, merged on `origin/master`)
`crates/`: proto+lma-framing, lma-identity, leaf-rns, radio-interface, lma-wire,
lma-lxmf, leaf-resource, leaf-power, sx126x; `host/`: interop, leaf-e2e.
Delivered as PRs T0–T9 (#168–#184); gates `rust-client/T*-GATE.md`. Host tests
green via cargo (`PROTOC` from the bazel cache for lma-wire). No work left here.

## RF leg (on-device) — **UNBLOCKED 2026-09-29**
- PR #185 radio `Interface` (merged), #187 `Sx1262` driver (merged),
  #188 esp-hal SPI `RadioBus` + firmware wiring (merged).
- **PR #189 `feat/rust-client-rfbeacon`** — on-air LoRa beacon: **PASS**.
  Every beacon `TX_DONE` in ~55 ms (exact airtime), continuous RX between
  beacons, RX frames dumped (len/rssi/snr/hex). Full evidence + root cause in
  `rust-client/RF-BEACON-GATE.md`.

## The blocker, resolved
The "endless TX" (mode 6 forever, no TX_DONE) was **spin-loop elision**:
`core::hint::spin_loop()` delays vanish at `opt-level = "s"`, so the IRQ poll
window (~15 ms) was shorter than the ~60 ms frame airtime — every TX was
abandoned mid-flight and then standby-aborted. Fixed by using real
`esp_hal::delay::Delay` waits everywhere. Driver additionally made byte-exact
with the proven MicroPython `sx126x.py` (STDBY_XOSC entry + settle, TCXO 15 ms
settle + error clear, 2-byte CLEAR_DEVICE_ERRORS, single-byte register RMW,
DS 15.1/15.2 workarounds, DIO IRQ masks, preamble 24).

## On-device facts (proven)
- TCXO is DIO3-powered and **required** (no-TCXO → XOSC dead, SET_TX EXEC_FAIL).
- SPI wire trace byte-identical to the reference; FIFO readback exact; PLL
  locks (FS mode 4); RTC alive; DIO1 IRQ line works.
- MicroPython path never runs `calibrate()` on this board (only under
  use_dcdc, which is false) — firmware mirrors that.

## Repo / tooling notes
- Branch: `origin/feat/rust-client-rfbeacon` (off `origin/master`).
- Flash: `espflash flash --before usb-reset --port /dev/ttyACM0 --chip esp32s3
  --after hard-reset <elf>` — no GO-button / bootloader dance needed.
- Build firmware: `source ~/export-esp.sh && cd rust-client/firmware &&
  cargo build --release` (esp nightly toolchain).
- Host tests: `cargo test --workspace` with
  `PROTOC=$HOME/.cache/bazel/.../external/protobuf~/protoc` (no system protoc).
- `~/.omp/agent/models.yml`: `opencode-go/kimi-k3`
  (base `https://opencode.ai/zen/go/v1`, key in `~/.omp/agent/.env`).

## Next steps (toward full RNS leaf firmware)
- Port the host `radio-interface`/`leaf-rns` crates into the firmware loop
  (driver is no_std already); the beacon loop already demonstrates the exact
  TX/RX cadence needed (continuous RX ↔ prepare_send/start_tx).
- Confirm server-side reception of the beacon at the production RNode.
