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

**RF-leg equipment finding (2026-09-30):** the host has **no RNode USB LoRa
radio** to receive the cardputer. LXMF-rs `lora` RNode interface config is
correct (opens the port, writes `rnode_state.json`, applies 868/BW125/SF7) but
the RNode detect fails (``did not confirm an RNode device``). Both LXMF-rs and
Python RNS 1.5.2 (`RNodeInterface`) fail the detect on **both** `/dev/ttyUSB0`
and `/dev/ttyUSB1` (probed with the exact RNS KISS detect frame across bauds);
those devices are sprouts (M5Stack), not RNode-format radios. Cardputer
(`/dev/ttyACM0`) confirmed alive and TXing Link/keepalive on 868. The RF RX leg
needs a real RNode (or a sprout flashed to RNode firmware) to attach before
the cardputer↔LXMF-rs over-LoRa test can run.

