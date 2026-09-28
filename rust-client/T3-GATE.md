# T3 — Radio Interface (Cardputer SX1262, esp-hal) + framing: evidence record

**Ticket:** T3 (#161). `rns_core::Interface` over the Cardputer SX1262 (SPI/esp-hal)
with fixed RF params + duty pacing; radio-generic for a future UART-AT DTU/Sprout
profile. **Accept:** host-test the frame RX/TX demux against the `lma_rnode_framing`
vectors; on-device radio loopback.

## Result: HOST PASS (framing) · on-device radio loopback PENDING-HARDWARE

The RNode LoRa RF framing (the wire format that interops with the production
mesh) is ported to Rust and host-tested; the SX1262 SPI driver / radio loopback
is the T3 hardware leg, held by the production-flash constraint.

## `crates/radio-interface` (design §5 `radio-interface`)

Faithful `no_std` + `alloc` port of `lma_rnode_framing` from the vendored
RTReticulum tree (the C++ reference was reverted off master in PR #173; the
canonical copy survives in `.rtreticulum`, whose header documents the measured
on-air wire behaviour this port encodes).

- `parse_rnode_frame(buf)` — 1-byte header: bit0 = SPLIT, upper nibble = tag.
- `SplitAssembler` — reassembles split LoRa frames into whole RNS packets,
  mirroring the C++ `RnodeSplitAssembler` **including the measured on-air rule**
  that a flagged frame opens a pair and the *next* frame completes it whatever
  its header says (RNS's own `isSplitPacket && seq == sequence` rule never
  assembles these — the wire example `0xcb`→`0x50`).
- `split_into_frames(payload)` — TX framing (>254 B payloads split; second half
  unflagged, matching on-air).
- `rf_params` — the fixed SX1262 settings (868/BW125/SF7/CR4:5/pre24/syncword
  0x1424/TCXO DIO3 1.8 V/CRC on) per design §4 + the Cardputer lora_interface.

7 host tests lock the semantics: header parse, non-split immediate complete,
flagged-then-next-frame-completes (the on-air `0xcb→0x50` case), stale-fragment
timeout retirement, oversize-partner drop, TX split → RX reassemble round-trip.
All via `bazel run //rust-client:test_radio`.

## Bazel-first
`crates/radio-interface` + `//rust-client:{radio_sources, test_radio}` (manual).

## On-device hardware leg (held)
The SX1262 SPI driver over esp-hal + an on-air loopback exercising `rf_params`
is T3's remaining acceptance. It needs the download-mode flash path (T1-GATE.md
mechanics) and the live Cardputer (production) — so it is **pending-hardware**
here, same constraint as the firmware NVS leg in T2. The framing + RF spec it
would exercise is fully host-tested.
