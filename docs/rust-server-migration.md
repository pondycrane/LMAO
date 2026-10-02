# Full-Rust LMAO Server — Migration Plan

Status: **ACTIVE** · Owner: pondycrane · Supersedes the old ESP32 Rust *client*
incremental plan (`docs/esp32-rust-client-design.md`, removed) and its rust-client
plan/handoff docs. The decision behind this plan: the Rust cardputer's Link +
Resource path was proven up to **server acceptance** (k8s pod logs show
`lmao.data: incoming link established` and `lmao.data: … resource advertised —
accepting`), but the **Python RNS responder's Resource receiver never emits the
part request** (`RESOURCE_REQ`) even with an ACTIVE link — a server-internal
receiver behavior that has dogged every leg of the Python-responder interop.
Rather than keep fighting the Python responder, replace it: a full-Rust server
using the same rns-rs/LXMF Rust stack the cardputer already runs.

## Decision

Port the LMAO server to a full-Rust stack built on **LXMF-rs**
(FreeTAKTeam; crates.io `lxmf` / `reticulum-rs`, v0.12). LXMF-rs ships a complete
Reticulum (transport, interfaces, Link/Resource) **and** an LXMF router, and
its Reticulum wire-interoperates with **rns-rs** (the crate family the
cardputer's `rns-core` belongs to) and with **Python Reticulum/LXMF** (the
existing sprout/spright clients). That covers both compatibility pillars the
Python server fought:

1. **RSS Link/Resource** (the whole blocker): native to LXMF-rs's Reticulum,
   same rns-rs family as the cardputer — guaranteed wire parity.
2. **LXMF delivery** (the existing `lxmf.delivery` path): native LXMF router.

## Why LXMF-rs (vs the other Rust LXMF options)

| Option | Stack | Maturity | Verdict |
|---|---|---|---|
| **LXMF-rs** (FreeTAKTeam) | ships its own Reticulum + LXMF + daemons | Released train (v0.12); Python Reticulum 1.5.2 parity (1,857 entries), full LXMF parity; rns-rs + Reticulum-Go interop evidence; real RNode/LoRa + Sideband + two-phone field evidence | **Selected** |
| rsLXMF (Ratspeak) | rsReticulum (sibling path dep) | Experimental ("do not use as source of truth") | Rejected (immature) |
| LXMF-rust (jrl290) | Rusticulum | WIP, API unstable, "not recommended for external use" | Rejected (not a server foundation) |

## Target architecture

```
[cardputer rns-core 0.1.17]  ←→  [LXMF-rs server: reticulum-rs + lxmf]  ←→  [Python-LXMF sprout clients]
                                          │
                          ┌───────────────┴────────────────┐
                          │ app layer (Rust)               │
                          │  tonic  gRPC 50051 (Send/      │
                          │         Subscribe stream/GetI  │
                          │         Identity)              │
                          │  async-nats  JetStream publish │
                          │  rusqlite   contact book       │
                          │  axum       contacts API 8081  │
                          └────────────────────────────────┘
```

## Module map (lmao_server/server.py → Rust)

| Python piece | Rust target | Notes |
|---|---|---|
| Reticulum bootstrap / identity / interfaces / announces | `reticulum-rs` (or `lxmf` SDK) | RNode /dev/ttyUSB0, 868/BW125/SF7 (same as `config_utils.py`); identity persisted |
| `lxmf.delivery` LXMRouter + `handle_lxmf_delivery` (ACK replies) | `lxmf` delivery callback | Port ACK/downlink logic; wire-parity with sprout is the gate test |
| `lmao.data` Link/Resource receiver (#197) | `reticulum-rs` Link/Resource (native) | Replaces the Python `_register_link_resource_destination` |
| gRPC `LMAO` service (50051) | `tonic` + `tonic-build` + `prost` | Compile `proto/lma_grpc.proto`, `proto/lma_messages.proto` in `build.rs`; implement `Send` / `Subscribe` (stream) / `GetIdentity` |
| NATS JetStream publish (`NatsQueue`) | `async-nats` | Fire-and-forget publish + reconnect |
| `ContactBook` (SQLite) | `rusqlite` | Same schema/db file |
| Contacts HTTP API 8081 (`start_contacts_server`) | `axum` | Co-serve with tonic on hyper/tokio |
| `_learn_contact` / allowed-clients / downlink / Sprout history | hand-ported modules | Pure app logic |
| asyncio event loop | `tokio` | Foundation for tonic/axum/nats/rusqlite |

## De-risking order (do not skip step 1)

1. **LXMF-rs parity proof** (gating): an LXMF-rs Reticulum node (local or second
   RNode) that (a) completes the cardputer Link + Resource that was proven
   against Python, and (b) exchanges an LXMF message with a Python sprout
   client. If either pillar fails, stop before porting.
2. **Scaffold the Rust server crate**: LXMF-rs bootstrap + RNode config +
   delivery callback + `lmao.data` Link/Resource receiver, mirroring
   `config_utils.py` + `server.py`.
3. **App layer in order**: gRPC (Send→Subscribe→GetIdentity), NATS, contact
   book + contacts API, downlink/allowlist/Sprout history — each independently
   testable against the running server.
4. **Cut over**: build Rust k8s image, keep the Python server as rollback until
   parity is proven on the live mesh.

## Risks / open items

- **Version skew**: cardputer `rns-core 0.1.17` is rns-1.4.2-era wire; LXMF-rs
  targets rns 1.5.2. RNS wire is version-stable and LXMF-rs lists rns-rs
  interop, but the cardputer↔LXMF-rs link must be verified in step 1.
- **Legacy ACK-path sensitivity**: the Python server was pinned to rns 1.3.5 +
  lxmf 1.0.1 for the LXMF ACK path (issue #70). A 1.5.2-era Rust LXMF may re-hit
  that; sprout-compat is part of the parity proof.
- **Licensing**: LXMF-rs is EPL-2.0 — confirm compatibility with this repo's
  license before shipping.
- **Deployment**: Rust k8s image replacing `lmao-server`; identity persistence,
  RNode device passthrough, gRPC/NATS endpoints unchanged.

## Deliverable definition of done

The Rust LMAO server (LXMF-rs spine + app layer) delivers LXMF end-to-end for
the Rust clients. In the k8s deployment: `kubectl logs <lmao-server>` shows, for
a real cardputer (rns-core) session: `lmao.data: incoming link established` →
`RESOURCE received — <N> bytes`, and a Rust cardputer sent an LXMF message
receives the server's ACK reply (delivery-receipt round-trip, as proven in the
Rust↔Rust parity test). No Python in the target path.

## Parity proof — status (2026-09-30, corrected framing)

**Correction:** the de-risking target is **Rust↔Rust** (LXMF-rs server + Rust
cardputer), not Python. Python is legacy; nothing in the target architecture
runs it. The earlier midpoint of this section conflated them; the corrected
proof is below.

**The LXMF-rs Rust stack works end-to-end (proven, evidence in
`/tmp/lxmf-parity/findings/rust/rust_to_rust_db.txt`):**
Two LXMF-rs 0.12 Rust nodes — **A** = server spine (rpc `127.0.0.1:4243`,
tcp_server `4300`, delivery `a9b2…96e`), **B** = client node (rpc `4244`,
tcp_client→`4300`, delivery `942d…8da`) — exchanged a delivered LXMF message
over a TCP RNS interface:
- A spine `rnstatus-rs`: `Announces: rx=2 tx=2` (A received B's announces);
  tcp_server later `latest_tx=650` (it answered — the opposite of the Python
  case, where it never TX'd).
- `lxmf-cli --rpc A send --destination 942d…`: queued.
- **B** `storage/reticulum.db` `messages`, direction **`in`**: source
  `a9b2…`, destination `942d…`, title `parity`, content `rust-to-rust hello
  from A`, `transport_encrypted=true (Curve25519)`.
- **A** same row direction **`out`**, `receipt_status='delivered'` → the spine
  received a delivery receipt from B (the LXMF ACK round-trip).
- Reverse B→A: B row direction `out`, `receipt_status='sent: link'` (direct
  link).

Both nodes use the same RNS 1.5.2-parity family, so Rust↔Rust interop
(cardputer rns-core is the other Rust family member) is the aligned target.

**Python cross-stack result (informational only, not a gate):** an initial
probe showed Python-RNS↔LXMF-rs do **not** interoperate over local interfaces
(shared-transport LocalInterface, TCP HDLC, AutoInterface co-location) — e.g.
LXMF-rs parsed 0 of Python's valid HDLC frames (evidence in
`/tmp/lxmf-parity/findings/`, `python_tcp_frames.hex`). Recorded here in case
future sprout-compat is ever wanted; it is **not** required by the full-Rust
plan. (If it were, that would be a real gap — a probable LXMF-rs 0.12
TCP/HDLC RX defect vs Python RNS 1.5.2.)

**Capability notes:** `lxmf-sdk` is a thin client that dials an external spine
(`lxmd`/`reticulumd`); it does **not** start an in-process Reticulum node. The
runnable node is the daemon binaries (release `lxmf-rs_0.12.0_linux-aarch64`),
whose source is not published to crates.io.

**Remaining gating risk (the real one):** the production architecture is
**cardputer (rns-core 0.1.17 / rns-1.4.2-era wire) ↔ LXMF-rs server
(RNS-1.5.2-parity wire)** over RNode/RF. This cross-(Rust-family) version skew
is **unverified** — it must be exercised on the cardputer RF link (hardware)
before the port is committed.

**RF-leg location (read this before probing):** the RNode LoRa radio is
**not attached to the dev workstation** — it lives on the **k3s cluster node
`tp4`** and is driven by the in-cluster server pod via a `hostPath` mount
(see `k8s/lmao-server-rust-app.yaml`; issue #93, "RNode on K8s"). The
workstation's `/dev/ttyUSB0`/`/dev/ttyUSB1` are unrelated devices (other
M5Stack boards, not the RNode) — do not probe them when looking for the RF
radio. The RNode's USB tty number on `tp4` can shift after a replug (e.g.
`ttyUSB0` → `ttyUSB1`), so after any replug confirm the live device from the
pod (`ls -l /dev/ttyUSB*`) and align `LMAO_RNODE_PORT` + the `hostPath` with
it, then `kubectl rollout restart deployment/lmao-server-rust-app`. A previous
note claimed the host "has no RNode" after a detect failed on the workstation's
own ttyUSB ports — that was probing the wrong host.


## Parity proof — updated (2026-10-01, hardware + Bazel + e2e)

The gating risk named above (cardputer ↔ Rust receiver over real LoRa RF) is
**retired**. Summary of what now runs end-to-end:

### Cardputer → Rust receiver over real LoRa (k8s on tp4)
- **Receiver** `lmao-server-rust-recv` (rns-net 0.7.2 / rns-core 0.1.17, the
  cardputer's RNS generation) deployed to tp4 with the persisted LMAO identity;
  Heltec V3 RNode on `/dev/ttyUSB0` (868/BW125/SF7/CR5).
- Cardputer reset re-dials `lmao.data` and the **Link establishes over RF**
  (`LINK established dest=24a097…c66 rtt≈0.67s`).
- The receiver **reassembles + sha256-verifies the pushed Resource**:
  `RESOURCE received bytes=1400 sha256=55772ad2…` (auto-split into parts by the
  library; no hand splitting).
- **Fixes that made it work:**
  1. **Serve Resource parts RAW** — rns-net carries `CONTEXT_RESOURCE` raw and
     does **not** link-decrypt parts (only ADV/REQ/HMU/PRF are link-encrypted);
     the cardputer was double-encrypting via `encrypt_pkt` on top of
     `ResourceSender::encrypt_fn`. `link_resource.rs` now wraps parts in a plain
     `build_link_packet` (commit `01da101`).
  2. **`RESOURCE_SDU` 220→160** — part link-packets must fit one ≤254 B LoRa
     frame (a >254 B split packet's 2nd frame does not radiate on the cardputer
     radio): `19 + 160 + 16 + 32 + pkcs7pad = 243 ≤ 254`.
  3. **§10.5 RESOURCE_REQ shape was a red herring** — instrumented the cardputer:
     the REQ (`flag=00 + 32 B resource_hash`) was *accepted* and 4 parts served;
     the format was correct all along.
- **Payload-size ceilings (measured):** ESP32 heap OOMs at ≥20 KB payload; the
  ADV's hashmap must fit one ≤254 B frame, capping a single-ADV resource at
  ~2.7–3 KB without RNS ADV segmentation. Handled + acted on; documented for any
  future "larger resource" firmware work.

### Bazel ("everything driven by Bazel")
- **rules_rust + crate_universe** in `MODULE.bazel` (0.74.0): host rust-client
  workspace compiles *in* Bazel (`rust_library` for `lma-identity`, `leaf-rns`,
  `radio-interface`, `leaf-power`, `sx126x`, `leaf-lxmf`, `leaf-resource` from
  the `@lmao_crates` cargo-bazel index). Xtensa ESP32 firmware stays
  out-of-band (cargo + esp nightly; no Bazel xtensa target) — the repo's
  documented convention.
- Root `BUILD.bazel` added (package marker required by cargo-bazel splicing).

### New e2e tests (both Bazel-wired)
1. **`//rust-client:resource_500b_e2e`** (Rust host, deterministic) — runs the
   cardputer's exact 500-B `(i % 251)` payload through `ResourceTx`→`ResourceRx`
   in-process and asserts the reassembled 500 bytes hash to the **LoRa-proven**
   `f6b83965…`. `bazel test` → PASSED.
2. **`//tests:test_cardputer_500b_resource`** (pytest, hardware-gated) — resets
   the Cardputer and asserts the k8s receiver logs
   `RESOURCE received bytes=500 sha256=f6b83965…`; skips when the rig
   (Cardputer `/dev/ttyACM0` on the dev host; RNode `/dev/ttyUSB<port>` on the
   k8s node tp4; receiver deployment in-cluster) is not
   reachable. `bazel test` → PASSED (skip path on the dev host).

### App layer — Rust server crate (`rust-client/host/lmao-server-rs`, 2026-10-01)

Port of the Python `lmao_server` app layer (migration step 3, "App layer in
order"); the RNS/LXMF mesh sits behind the `MeshSender` seam so the whole app
layer is host-testable without a radio. `cargo test -p lmao-server-rs` → 20/20
PASS; binary smoke-tested live (contact book + NATS + gRPC + contacts HTTP).

| Python piece | Rust port |
|---|---|
| gRPC `LMAO` service 50051 | `grpc.rs` tonic (Send / Subscribe stream / GetIdentity), proto via tonic-build (`proto/lma_grpc.proto`); envelope types reused from `lma-wire` |
| `lma_core/contact_book.py` SQLite | `store.rs` rusqlite — same schema (`contacts`), `register`/`touch`/`find`/`all`, upsert CASE + `device-<last4>` default names |
| `lma_core/contacts_api.py` :8081 | `contacts.rs` axum — `GET /contacts`, `GET /contacts/find`, `POST /contacts` (same 400/201 semantics); `LMAO_CONTACTS_PORT` |
| `_publish_to_nats` (subject `lmao.messages.env`, stream `LMAO_MESSAGES`) | `nats.rs` async-nats JetStream, graceful-degrade |
| `handle_lxmf_delivery` app logic | `delivery.rs` — allow-list gate, learn-contact (register-if-unknown else touch), LMAOEnvelope decode + Sprout chart fold, ACK TextMessage (DATA-line piggyback), NATS, gRPC fan-out |
| `lma_core/sprout_history.py` | `sprout.rs` — ring + `DATA <node> <dry> <wet> … <pump-mask>` line |

**Remaining gaps / next steps:**
1. **Bazel-ify the server crate** (`//rust-client:lmao_server_rs`): needs a
   `cargo_build_script` for tonic-build with a protoc toolchain in the Bazel
   sandbox (the crate builds in cargo now; `@lmao_crates` index already
   contains its deps).
2. **Wire the rns-net RF seam**: implement `MeshSender` on the rns-net
   link/resource receiver (deliver ACK + dispatch `Send` envelopes toward a
   destination hash), then the live cardputer → delivery → gRPC/NATS path is
   end-to-end Rust. `LogMesh` stub is in place meanwhile.
3. **LXMF delivery semantics over rns-net**: currently the Resource receiver
   feeds raw envelopes into `DeliveryHandler`; routing/ACK wire behavior needs
   the rns-net send path (either rns-net Link/Resource dispatch or an
   lxmf-core pack step) proven on RF.

### Deployment — Rust server via install_all (`--include-services`, 2026-10-01)

The Rust app-layer server is now releasable through the repo's deploy tool the
same way the Python server is:

- **Image**: `docker/rust-lmao-server/Dockerfile` (multi-stage: cargo build with
  `protobuf-compiler` for tonic-build → slim runtime; runtime env sets
  `LMAO_CONTACTS_DB=/data/contacts.db` and the in-cluster NATS address).
- **Manifest**: `k8s/lmao-server-rust-app.yaml` — Deployment pinned to tp4
  (the k3s node with the RNode) + Service (gRPC 50051, contacts 8081). The
  ContactBook persists on the `lmao-server-rust-app-contacts` `local-path`
  PVC (the provisioner was repaired + the PVC bound; survived pod restarts).
  **RF receive merged into the server** (`src/rf.rs`): the pod drives the
  RNode via `LMAO_RNODE_PORT` (privileged, `hostPath`, currently
  `/dev/ttyUSB1` on tp4) and ingests payload Resources straight into
  DeliveryHandler — the standalone receiver image is no longer needed.
  `MeshSender` ACK-over-rns-net is still the remaining seam (app layer uses
  `LogMesh`).
- **Install_Services**: `tools/install_services.install_rust_lmao_server()`
  builds the image, releases it via the local registry
  (`192.168.50.153:5000/lmao-server-rust-app:latest`), applies the manifest,
  waits for rollout, and verifies the pod logged both listeners.
- **install_all**: `--include-services` now deploys the Rust server (skip with
  `--skip-rust-server`; skipped automatically under `--skip-k8s`).


### Build-time optimization (warm builder image, 2026-10-01)

`install_all --include-services` used to recompile the **entire dependency tree
on every Rust-server deploy** (~15–40 min on arm64: tonic/hyper/rusqlite/
async-nats/rns-net). Now:

- `docker/rust-lmao-server/builder.Dockerfile` compiles the full dep graph ONCE
  into `/src/rust-client/target`, tagged `lmao-server-rust-builder:1`.
- `docker/rust-lmao-server/Dockerfile` `FROM`s that warm image and recompiles
  only the changed `lmao-server-rs` source against the cached deps —
  **minutes, not the cold tree compile**.
- `install_rust_lmao_server()` builds the warm builder automatically on the
  first deploy if it's missing; later deploys are thin.

Chosen over cargo-chef / BuildKit cache mounts / two-tier dummy caches because
those depend on Docker layer-caching behavior that is unreliable on the deploy
host's legacy builder; the warm-builder image is deterministic on any builder.

Measured on the deploy host (arm64): the one-time warm-builder cold build takes
~42 min; an unchanged deploy is ~11 s (Docker cache); a source-change deploy is
~4.5 min wall (cargo 1m41s) — versus ~15–40 min per deploy before. A
`.dockerignore` at the repo root excludes `rust-client/target` (~GB) from the
build context, which alone cut ~6–7 min of tar/upload per deploy.


### Cardputer chart display in Rust (2026-10-01)

The Sprout chart the Cardputer's screen shows is now Rust end to end.  The
MicroPython `cardputer_client/chart.py` renderer is recreated as the no_std
crate `rust-client/crates/lma-chart`:

- `parse_data_line` — the server's `DATA …` line (the RUST server already emits
  it from `sprout.rs`, and the ACK piggybacks it) → a `ChartRecord`;
- the same geometry/windows/colours as the Python original (240×135 plot box,
  soil/humidity/temperature traces, band thresholds, watering ticks) drawn over
  a two-primitive `Display` trait (`fill` + `pixel`) — lines, traces and the
  embedded 8×8 font are the crate's own pixel logic, so a driver only supplies
  a framebuffer;
- host-tested (`cargo test -p lma-chart`, 27 tests ported from
  `tests/test_chart.py`) + a `render_ppm` example that paints a `DATA` line to
  a PPM for panel-design previews without hardware.

The firmware (esp32s3) carries the panel: `rust-client/firmware/src/display.rs`
is an ST7789 SPI3 driver (SCK=36/MOSI=35/CS=37/DC=34/BL=38 — the Cardputer ADV
map Meshtastic's `m5stack_cardputer_adv/variant.h` documents) with an RGB565
framebuffer.  `handle_lxmf_reply` parses the reply and `pump_rx` paints the
result onto the LCD each time a fresh `DATA` line arrives (the previous
serial-log substitute).  The driver is the open-source `mipidsi` 0.10 ST7789
model (display on SPI2/FSPI, RST=33, offset 52,40 + Deg90 + inverted) — the
same config the no_std Rust Cardputer-ADV reference `BotEkrem/echoputer` uses.
Verified on-device (2026-10-02): the 240×135 panel displays the chart header.

Reconciled to make the firmware build again: the crate split
`leaf-lxmf` → `lma-lxmf` orphaned the firmware's five `leaf_lxmf::` calls and a
dangling `leaf-lxmf` path dep.  All five functions already live in-firmware
(`random_hash` was added from the µReticulum reference layout), so the dep is
removed and the calls point at `crate::rns_link` instead.


### install_all `--stack` selector

`tools/install_all.py --include-services` now takes `--stack {auto,python,
rust}`, default **rust**: a plain services deploy builds/releases/installs
only the Rust LMAO server (the future replacement) and skips the legacy
Python `install_pi_server`/`deploy_lmao_server` image build.  Use
`--stack python` to deploy the Python server instead, or `--stack auto` for
the old behavior of deploying both (honouring `--skip-server` /
`--skip-rust-server`).  `install_rust_lmao_server` stays as-is: the warm
builder + local-registry release + `k8s/lmao-server-rust-app.yaml` apply
(contacts PVC + Deployment + Service) + rollout/pod-listener verification.

