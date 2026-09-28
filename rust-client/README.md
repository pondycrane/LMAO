# rust-client — the LMAO ESP32-S3 Rust client

The Rust implementation of the LMAO **Cardputer leaf** (design:
[`docs/esp32-rust-client-design.md`](../docs/esp32-rust-client-design.md)).
The standing mandate: the MicroPython Cardputer stays the live production node
until the Rust stack is end-to-end; on-device legs are production-gated.

## Layout (design §5 — data-flow)

```
radio (esp-hal UART/SPI)
   └► radio-interface   rns_core::Interface framing (RNode LoRa / SX1262): T3
        └► leaf-rns     RNS transport + Link manager: establish/serve/sustain,
        │               link-window scheduler (duty-cycled rendezvous): T4
        └► leaf-resource  RNS Resource RX/TX over the Link; hash-verify,
                         resume across dormant gaps: T6
                         └► lma-wire   prost LMAOEnvelope/SensorReport (control): T5
                              └► lma-lxmf  LXMF pack/sign/verify control path: T5
```

| Path | Crate | Status |
|---|---|---|
| host/interop | Rust↔Python RNS Link+Resource interop harness | T0 — PASS |
| firmware/ | no_std esp-hal boot (heartbeat) | T1 — boots on Cardputer |
| crates/lma-identity | RNS identity + `lxmf/delivery` DEST | T2 — host PASS |
| crates/radio-interface | RNode LoRa frame demux/RF-framing | T3 — host PASS |
| crates/leaf-rns | Link-window scheduler + resume queue | T4 — host PASS |
| crates/lma-wire | prost `LmaoEnvelope`/`SensorReport` (from `proto/lma_messages.proto`, §6b) | T5 — host PASS |
| crates/lma-lxmf | LXMF control path (pack/sign/verify) | T5 — host PASS |
| crates/leaf-resource | RNS Resource over Link (rx/tx, resume) | T6 — host PASS |
| crates/lma-framing | LMAF/successor framing reassembler on Resource (manifest→chunks→crc→verify→ack) | T8 — host PASS |
| crates/leaf-power | power/duty polish: sleep-to-window, watchdog, heap guard | T9 — host PASS |
| host/leaf-e2e | **composed leaf pipeline** (§5 data-flow, host) | T7 — PASS |

## Gates

Host (Bazel-first, all `manual` sh_binary wrappers, no rules_rust):

```bash
bazel run  //rust-client:run_leaf_e2e            # T7: composed leaf pipeline
bazel run  //rust-client:test_lma_identity        # T2: identity + DEST goldens
bazel run  //rust-client:test_radio               # T3: RNode framing
bazel run  //rust-client:test_leaf_rns            # T4: link-window scheduler
bazel run  //rust-client:test_lma_wire            # T5: prost ≡ Python golden (needs PROTOC)
bazel run  //rust-client:test_lma_lxmf            # T5: LXMF control path (needs PROTOC)
bazel run  //rust-client:test_leaf_resource       # T6: Resource over Link + RNS hashes
bazel run  //rust-client:test_lma_framing         # T8: LMAF framing on Resource (needs PROTOC)
bazel run  //rust-client:test_leaf_power          # T9: power/duty polish
bazel run  //rust-client:run_t0_gate -- --python <py>   # T0: live Python RNS interop
```

- `lma-wire`/`lma-lxmf`/`leaf` need `protoc` (prost-build from
  `proto/lma_messages.proto`): `export PROTOC=/path/to/protoc` (pinned
  `protoc-27.3-linux-aarch_64`; see `UPSTREAM.md`).
- Hardware: the Cardputer is flashed only via
  `bazel run //cardputer_client:flash` (stable MicroPython — do not disturb the
  production node). The Rust firmware is `//rust-client:{build_firmware,
  flash_firmware}` (T1). Per-ticket evidence: `T0-GATE.md` … `T7-GATE.md`.

## Status

Host: the whole leaf control + resource pipeline links and runs (T7 host PASS).
On-device: the Rust **RF leg (radio-interface → link → resource over the real
RNode to the production server)** is pending-hardware — it needs firmware
transport bring-up + an attached RNode, and is production-gated.
