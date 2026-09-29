# RF-leg step 6 — RNS link in firmware (driver under RadioInterface): evidence

Wires the SX1262 driver under the `radio-interface` `RadioInterface` contract +
the RNode RF framing — the MicroPython `lora.py` transport path ported to Rust
no_std. The Cardputer now sends and receives **real RNS packets** on the
production mesh profile.

## Result: PASS (on hardware, 2026-09-29)

TX (valid RNS DATA packet, arbitrary payload, every beat):
```
[rns] beat #3 TX 31 B link_tx=3
...
[rns] beat #46 TX 31 B link_tx=46
```
RX (real mesh traffic decoded through the interface):
```
[rns] rx frame n=167 split=false rssi=-27dBm snr=12dB complete=true stale=false
[rns] RNS packet len=167 type=0x1 hops=0 dst=[3a,82,98,32,cf,a9,bb,96] hash=[...] dist_rssi=Some(-27)
[rns] RNS packet len=284 type=0x5 hops=104 dst=[fa,46,d9,24,a0,16,c7,84] ...
[rns] RNS packet len=51 type=0x8 hops=0 dst=[6b,9f,66,01,4d,98,53,fa] ...
```
The 51-B `dst=6b9f66...` packets are the **nearby sprout leaf** (its own log
has the same `dh=6b9f66014d9853fa...`), i.e. genuine cross-node RNS interop at
-68…-72 dBm. Split-fragment reassembly is alive (`split=true` frames held for
their partner). RSSI/SNR are stamped on decoded packets.

Residual: ~4 % of TXes hit the RTC-timeout (the intermittent sequencer stick
from step 5) and are self-recovered by hard-reset + reconfigure; the link
continues. Frame split/reassembly is exercised.

## What this adds
- `firmware/src/rns_link.rs` — `RnsLink<Sx1262>`: `send()` = `split_into_frames`
  → bounded `start_tx` → `TX_DONE` → re-arm RX; `pump_rx()` = `parse_rnode_frame`
  → `SplitAssembler` → `RadioInterface::process_incoming` (RawPacket decode +
  RSSI/SNR stamp).
- `firmware/src/main.rs` — no_std heap (linked_list_allocator over 32 KiB
  static), builds a valid RNS DATA packet via `rns_core::packet::RawPacket::pack`
  carrying arbitrary bytes, TX every beat, listen + decode in the idle window.
- **no_std RNS stack on xtensa**: `radio-interface` + `rns-core` + `rns-crypto`
  cross-compile for the Cardputer.
  - `radio-interface` depends on `rns-core` with `default-features = false`
    (rns-core's default enables `std`).
  - `rns-crypto` vendored with `default = []` (it is no_std behind its optional
    `std` feature; see `vendor/rns-crypto`, wired via `[patch.crates-io]`).

## Host evidence
`cargo test --workspace` green (radio-interface framing goldens + all suites).

## Hardware
Flash: `espflash flash --before usb-reset --port /dev/ttyACM0 --chip esp32s3
--after hard-reset target/xtensa-esp32s3-none-elf/release/lmao-firmware-t1`.

## Next
- Real RNS identity/transport (replace the bootstrap `0xFF…` destination hash
  + fixed DATA packet with the leaf's RNS destination + announce).
- Keepalive/announce pacing and the leaf-rns duty-cycle window scheduler.
