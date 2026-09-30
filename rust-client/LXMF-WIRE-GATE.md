# LXMF-WIRE-GATE — opportunistic wire-format diagnosis + fix contract (evidence)

**Date:** 2026-09-29 · **Result: root cause FOUND (offline sim PASS)** · Fix tracked as #197 (L1) / #198 (L2); live verification #199–#201.

## Question

Why does the production server never log `Message received` for the Rust
Cardputer client's LXMF messages (handoff criterion A), despite correct
identity, allow-list, announces, and encryption?

## Method

Offline simulation on the selfhost rig: stock **Python RNS 1.3.5 + LXMF 1.0.1**
(the production pins) with the **real server identity**
(`~/.local/share/lmao_server/lxmf/identity`), ingesting packet bytes produced
by the exact Rust pipeline (rns-core 0.1.17 / lxmf-core 0.1.5 / rns-crypto
0.1.10, production constants from `firmware/src/rns_link.rs`) through
`RNS.Transport.inbound` — harness: `rust-client/host/lxmf-sim/ingest.py`.

## Evidence

| Input | Result |
|---|---|
| Rust `lxmf.delivery` announce | **Valid announce** logged; `Identity.recall(99ce32…) → 8a1766…`; path installed |
| DATA packet, old format (encrypt full `packed`, dest hash included) | `Could not assemble LXMF message from received data` (NOTICE) → **dropped**, no delivery callback |
| DATA packet, fixed format (encrypt `packed[16:]`) | **Delivery callback fires**: `From: 99ce32311dc37193eff4951a912f8f1b  Title: p:Envelope  Content length: 86` |

## Root cause

Python LXMF opportunistic delivery sends
`RNS.Packet(dest, packed[LXMessage.DESTINATION_LENGTH:])` — the encrypted RNS
payload omits the 16-byte destination hash; the receiver re-prepends it from
the packet header (`LXMRouter.delivery_packet`:
`lxmf_data = packet.destination.hash + data`). The Rust firmware encrypted the
full packed message, misaligning the server's `LXMessage.unpack_from_bytes`
(src parsed as the dest hash → msgpack garbage → drop).

RNS-layer decryption succeeded all along — consistent with the observed zero
`Decryption with ratchets failed` lines. The failure was silent unless you
grep for `Could not assemble LXMF message`.

## Disproven on the way (do not reopen)

- **Client-side RNS transport/routing is not required.** RNS 1.3.5
  `Transport.inbound` delivers single-hop DATA to a matching local IN
  destination with no path entry (`_handle_data` → `destinations_map`); a
  HEADER_2/transport frame would be dropped as transit-for-someone-else on
  this mesh (no transport nodes).
- Identity allow-list, ratchets, LXMF source hash — all verified fine
  (announce validation + recall above).

## The fix contract (what L1/L2 must implement)

- **Outbound:** RNS DATA payload = `encrypt(packed[16..])` where `packed` is
  the lxmf-core wire message (`dest‖src‖sig‖payload`).
- **Inbound:** `lxmf_wire = our_lxmf_delivery_hash ‖ decrypt(payload)` before
  `lxmf_core::message::unpack`.
- Announce `random_hash` = `urandom(5) ‖ unix_time(5, BE)` (reference layout).
