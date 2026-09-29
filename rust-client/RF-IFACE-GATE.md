# RF-leg (T7 hardware) step 1 — the leaf radio Interface contract (+rssi wire)

The on-device half of the milestone: getting the Rust leaf speaking the RNS wire
over the Cardputer's SX1262. This PR is **step 1**: the **leaf radio `Interface`
contract**, cloned lean per the directive — no `rns-net` (std mesh router)
pulled onto the leaf; only the interface contract the transport needs, as the
µReticulum leaf already defines it.

## Design decision — "clone only the interface"
`rns-net 0.7.2` has **no clean `Interface` trait** to clone: its interface
machinery is std-only (threads, `RawFd`, `io::Result`, worker loops, a `Frame`
event bus). The lean, wire-faithful source is the repo's own **µReticulum
`Interface`** (`cardputer_client/lib/urns/interfaces/__init__.py`) — the same
leaf-class device, already minimal:
`process_incoming(data)` → transport; `process_outgoing(data)`; flow metadata
(`online/enabled/mode/bitrate/mtu/HW_MTU/OUT/IN`), signal (`rssi`/`snr`), stats.

`rns-core` (no_std, already pinned `0.1.17`) supplies the decode the interface
hands to the protocol core (`RawPacket::unpack` — HEADER_1/2, dest, context,
data, `packet_hash`), and **already carries `rssi: Option<i16>`/`snr:
Option<f32>`** — the exact µReticulum `packet.rssi = interface.rssi` stamp.

## What this PR adds (host-verified; nothing invented)
`crates/radio-interface/src/interface.rs` (no_std+alloc; `rns-core` dep added):
- `InterfaceMode` (µReticulum `MODE_*`), `FlowStats` (rx/tx bytes+frames,
  last-activity), `RadioInterface` (name/online/enabled/mode/bitrate/mtu/
  hw_mtu/out/inn/**rssi/snr**; defaults match the reference).
- `process_incoming(data, now_ms)` — tally flow stats, `RawPacket::unpack`, and
  **stamp `rssi`/`snr` onto the packet** (the feed for the sensor_id 9 RSSI
  `SensorReport`).
- `note_outgoing(data)` / `close()`.

Host tests (`//rust-client:test_radio`, 5 new):
`reference_defaults_match_microreticulum`, `process_incoming_stamps_interface_signal_on_packet`,
`raw_packet_roundtrip_pack_then_inbound_decode` (HEADER_2 keeps transport_id),
`header1_fits_the_464_byte_opp_budget`, `outgoing_tally_and_close`.

## Grounded (no guessed pins)
Cardputer SX1262 (repo `cardputer_client/firmware/main/cardputer_pins.h` +
`lora_boards.py`): SCK=40, MOSI=14, MISO=39, CS=5, BUSY=6, DIO1=4, RST=3 (HSPI;
GPIO40/39 are JTAG legs to reclaim); RF 868/BW125/SF7/CR4:5/pre24/syncword
0x1424; DIO2=RF-SW; DIO3-TCXO.

## Bazel-first
Reuses the existing `//rust-client:{radio_sources, test_radio}` (manual).

## Next steps (RF-leg, hardware)
The SX1262 esp-hal SPI driver implementing this interface, the transport/link
glue over it, then the bootloader flash against the production server RNode.
