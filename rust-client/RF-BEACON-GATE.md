# RF-leg step 5 — on-air LoRa beacon TX probe: evidence

Continues the RF leg toward real on-air connectivity. Step 4 (PR #188) confirmed
the SX1262 **configures** on the Cardputer; **this adds the first transmission**:
transmit one LoRa frame and confirm the radio reports `TX_DONE`.

## What this PR does
- `firmware/src/main.rs`: after the radio config, **`prepare_send` + `start_tx` a
  7-byte beacon** (RNode 1-byte header `0xcb` + `LMAO` payload) on the fixed
  profile, then **poll `get_irq_status` for `TX_DONE`** and log
  `[t3] beacon 7B TX_DONE=<bool>`.
- This is the on-air-transmission probe AND it exercises the SX1262 **read path**
  (`get_irq_status`, whose response layout RF-step 3 flagged as
  hardware-verify-required). A `TX_DONE=true` confirms both a real transmission
  left the radio and the IRQ read layout is correct.

## Build evidence
`cargo build --release` (esp 1.97 nightly) clean; image 238 184 → **238 952 B**.

## Hardware (flash) leg
Flash in bootloader mode (`--before no-reset`) and read the boot log:
- `[t3] beacon 7B TX_DONE=true` → real on-air TX + IRQ-read layout OK.
- `TX_DONE=false` (or a `[t3]` error) → read-layout or TX issue to chase on the
  target. Server-side reception at the RNode is the subsequent step.
