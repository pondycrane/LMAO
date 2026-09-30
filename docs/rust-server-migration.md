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

`kubectl logs <lmao-server>` shows, for a real cardputer session:
`lmao.data: incoming link established` → `RESOURCE received — <N> bytes`, and a
Python sprout still receives LXMF ACK replies.
