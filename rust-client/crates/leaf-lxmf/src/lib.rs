//! LMAO leaf **LXMF wire format** — the pure, host-testable protocol logic that
//! the firmware's `rns_link.rs` used to inline. Built directly on the pinned
//! rns-core / lxmf-core / rns-crypto crates (no hand-ported protocol code), so
//! the opportunistic wire contract is pinned by host tests and shared verbatim
//! by the on-device firmware.
//!
//! # The contract this crate pins (see `LXMF-WIRE-GATE.md`, issue #197 / #198)
//!
//! Python LXMF 1.0.1 opportunistic delivery sends
//! `RNS.Packet(dest, packed[LXMessage.DESTINATION_LENGTH:])`: the encrypted RNS
//! payload carries the LXMF wire message **without** the leading 16-byte
//! destination hash, and the receiver re-prepends it from the packet header
//! (`LXMRouter.delivery_packet`: `lxmf_data = packet.destination.hash + data`).
//!
//! - **Outbound** [`wire::pack_lxmf_to_server`]: encrypt `packed[16..]`, so the
//!   server's `unpack_from_bytes` realigns and delivers (not
//!   "Could not assemble LXMF message from received data").
//! - **Inbound** [`inbound::decrypt_server_reply`]: prepend OUR `lxmf.delivery`
//!   hash to the decrypted payload before `lxmf_core::message::unpack` (the
//!   mirror of the same rule).
//! - **Announce** [`announce::random_hash`]: `urandom(5) ‖ unix_time(5, BE)`
//!   so the server's path-table timebase is real time.

#![no_std]

extern crate alloc;

pub mod announce;
pub mod envelope;
pub mod inbound;
pub mod wire;

// Re-export the headline types at the crate root (the firmware + tests use
// them without caring about the module split).
pub use announce::{announce_for, build_announce, random_hash};
pub use envelope::{build_text_envelope, decode_text_content};
pub use inbound::{
    decrypt_server_reply, parse_data_line, roundi, ChartRecord, Reply,
};
pub use wire::{
    build_lxmf_to_server, build_message_to_server, lxmf_delivery_hash,
    pack_lxmf_to_server, PackedEnvelope,
};
