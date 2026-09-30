# LXMF Cardputer-Client — Completion Plan (Rust no_std)

Status snapshot: **2026-09-29**. Companion to `LXMF-CLIENT-HANDOFF.md` (the
resume doc); this is the forward ticket plan, tracked as GitHub issues under
epic **#157**. The acceptance bar is the handoff's A/B/C criteria (≥6 h at the
30 s cadence, verified on **device serial + server pod logs**).

## Corrected diagnosis (evidence-first, supersedes the handoff's "transport" theory)

The handoff's blocker — *"client needs RNS transport/routing for delivery"* —
is **disproven** by the reference implementation and by experiment:

1. **Single-hop delivery needs no path/transport.** Python RNS 1.3.5
   `Transport.inbound` delivers any DATA packet whose destination hash matches
   a local IN destination (`_handle_data` → `destinations_map` lookup; urns
   port agrees). There is no transport node between the Cardputer and the
   server's RNode — a HEADER_2/transport frame would only be dropped as
   transit-for-someone-else.
2. **The actual bug is the LXMF opportunistic wire format.** Python LXMF sends
   `RNS.Packet(dest, packed[LXMessage.DESTINATION_LENGTH:])` — the encrypted
   RNS payload carries the LXMF wire message **without the leading 16-byte
   destination hash**; the receiver re-prepends it from the packet header
   (`LXMRouter.delivery_packet`: `lxmf_data = packet.destination.hash + data`).
   The Rust firmware encrypted the **full** packed message (dest included), so
   the server's `unpack_from_bytes` misaligned (src = dest hash, msgpack
   garbage) and LXMF dropped it: *"Could not assemble LXMF message from
   received data"* (NOTICE) — the line the handoff's greps never looked for.
3. **Experiment (offline sim, this machine):** stock Python RNS 1.3.5 + LXMF
   1.0.1, real server identity, packet bytes built by the exact Rust pipeline
   (rns-core/lxmf-core/rns-crypto, production constants):
   - Rust announce → **valid**: `Identity.recall(99ce32…)` + path installed.
   - Old-format DATA → `Could not assemble LXMF message from received data`, no
     delivery.
   - Fixed-format DATA (encrypt `packed[16:]`) → **delivery callback fires**:
     `From: 99ce32311dc37193eff4951a912f8f1b  Title: p:Envelope  Content length: 86`
     — the production "Message received" line.

So: identity, allow-list, announces, encryption, and RF framing were all fine;
only the LXMF slice was wrong. No transport work is required for criterion A.

## Tickets (GitHub issues)

- **L1 (#197) — Fix the LXMF opportunistic wire format (outbound)**. Encrypt
  `packed[16..]` in `build_lxmf_to_server`. Also align the announce
  `random_hash` with the reference layout (`urandom(5) ‖ unix_time(5, BE)`;
  the firmware currently sends 10 fully random bytes — accepted, but the
  path-table timebase it produces is garbage). Extract the pure wire logic
  from `firmware/src/rns_link.rs` into **`crates/leaf-lxmf`** (no_std, alloc;
  on rns-core/lxmf-core/rns-crypto — no re-ported protocol code) so the
  contract is host-testable; regression tests pin the opportunistic slice.
- **L2 (#198) — Inbound mirror fix + reply decode**. The server reply is the same
  opportunistic shape, so `handle_lxmf_reply` must prepend our
  `lxmf.delivery` hash to the decrypted payload before
  `lxmf_core::message::unpack`. Host test simulates the server→client reply
  (pack with server identity, encrypt `packed[16:]`, verify + parse the
  `ACK …\nDATA …` chart line).
- **L3 (#199) — Verify A live (outbound ≥99%)**. Flash the fixed firmware; server pod
  logs show `Message received — From: 99ce3231…` + `Reply sent.` at the 30 s
  cadence. Historical-evidence check: the pod should show past
  `Could not assemble LXMF message` NOTICEs at our beat times (confirms the
  diagnosis against production).
- **L4 (#200) — Verify B (inbound ≥95%)**. Device serial shows `INBOUND lxmf …
  sig_valid=True`, `reply: ACK …`, `CHART node=…` per cycle.
- **L5 (#201) — Durability window C (≥6 h)**. Continuous bidirectional flow, no
  resets, no heap-growth halt (128 KiB static heap), no radio wedge; record
  uptime + per-cycle A/B success in the handoff.

Non-goals (explicitly, per the corrected diagnosis): no rns-core
transport/routing integration, no Link/Resource work (that's T6 on the main
epic), no changes to the MicroPython production client.

## Risks / watch items

- **Reply path needs our announces.** The server can only encrypt its reply
  after recalling our identity from a valid announce — announce cadence
  (~20 s alternating) covers this; keep it ahead of the first message.
- **Half-duplex RX window** (200 ms/1 s duty) may miss the server's reply —
  L4 measures the real rate; widening the window is the knob if B underperforms.
- **Heap churn** at 128 KiB: L5 watches for the fragmentation halt seen on the
  earlier 32 KiB build.
