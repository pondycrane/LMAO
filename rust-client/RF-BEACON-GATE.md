# RF-leg step 5 — on-air LoRa beacon TX probe: evidence

Continues the RF leg toward real on-air connectivity. Step 4 (PR #188) confirmed
the SX1262 **configures** on the Cardputer; **this adds the first transmission**:
transmit one LoRa frame and confirm the radio reports `TX_DONE`.

## Result: PASS (on hardware, 2026-09-29)

```
[t3] sx1262 configured 868/BW125/SF7/CR4:5 pre24 syncword=0x1424
[t3] reg740(readback LSYNCRH)=0x1424
[t3] continuous RX armed
[t3] beacon #1 result=TX_DONE polls=11
[t3] beacon #2 result=TX_DONE polls=11
... (every beacon TX_DONE; 11 polls × 5 ms ≈ 55 ms = exact frame airtime)
```

`TX_DONE` on every beacon, DIO1 asserted (checked during bring-up), radio
returns cleanly to STDBY, then re-arms continuous RX and dumps any received
frame (len/rssi/snr/hex) — the bidirectional RNS path.

## Root cause of the "endless TX" blocker (was: mode 6 forever, no TX_DONE)

**Not a radio problem — a host timing bug.** The IRQ poll loop idled on
`core::hint::spin_loop()` delays, which LLVM elides at `opt-level = "s"`.
Depending on surrounding code, some builds kept the spins (~700 ms poll
window — TX_DONE seen) and some elided them (~15 ms poll window — **shorter
than the ~60 ms frame airtime**, so the loop always gave up mid-flight, and
the recovery code then standby-aborted the healthy TX). Flip-flopping
behavior across builds was pure code-layout luck.

Verified on-device while chasing this (all healthy): SPI wire trace
byte-identical to the reference driver, FIFO readback exact, STDBY_XOSC
entered, PLL locks (FS mode 4), RTC alive (1-tick timeout aborts instantly),
DIO1 IRQ line, RX holds mode 5.

## Fixes that came out of it (driver + firmware)

Driver (`crates/sx126x`) — now byte-exact with the proven MicroPython
`sx126x.py`:
- `prepare_send`/`start_rx` enter **STDBY_XOSC** (was STDBY_RC) with a 5 ms
  XOSC settle + device-error clear (XOSC restarts on every RC→XOSC
  transition on this DIO3-TCXO module).
- `set_dio3_as_tcxo`: 15 ms settle + clear the expected XOSC_START_ERR
  (reference behavior). **TCXO is required** — without DIO3 power the XOSC
  never runs (SET_TX EXEC_FAIL), i.e. the Cap LoRa-1262 clock is a
  DIO3-powered TCXO.
- `CLEAR_DEVICE_ERRORS` now sends the 2-byte 0x0000 argument per DS 13.4.3
  (was a short frame that may not execute).
- Single-byte `reg_read_u8`/`reg_write_u8` — the old `write_register_u8`
  wrote two bytes and clobbered the adjacent register (0x088A).
- DS 15.1 MODQUAL workaround is a single-byte read-modify-write; added the
  DS 15.2 `0x8D8 |= 0x1E` TX-clamp workaround; added `set_dio_irq_masks`
  (TX/RX/timeout → DIO1) and `clear_irq`.
- Preamble 24 (was 8) — the actual leaf profile.
- `RadioBus` gained `delay_ms` (reset pulse 1 ms/5 ms, TCXO/XOSC settles).

Firmware (`firmware/`):
- Init order byte-exact with the reference: reset → DIO2 → DIO3 TCXO → IRQ
  masks → clear IRQ → packet type → freq → syncword → PA (14 dBm optimal,
  40 µs ramp) → modulation → DS 15.2 workaround. No calibrate (the working
  MicroPython path never calibrates on this board).
- **All waits are real `esp_hal::delay::Delay` milliseconds** — no spin
  loops anywhere (the root-cause fix).
- TX uses a bounded 200 ms RTC timeout so a wedged sequencer is force-aborted
  instead of hanging; on failure the radio is hard-reset + reconfigured.
- Periodic beacon (~2 s cadence) with continuous RX idle between beacons;
  received frames are dumped (len/rssi/snr/hex).
- Flashing without the GO-button dance: `espflash flash --before usb-reset`
  works over the native USB-Serial-JTAG.

## Host evidence
`cargo test --workspace` green (12/12 sx126x goldens updated to the new
byte-exact sequences; full workspace suites pass).
