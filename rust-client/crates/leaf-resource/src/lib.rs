//! T6 — RNS Resource over Link on the leaf (design §5 `leaf-resource`).
//!
//! A leaf-facing wrapper over `rns-core`'s `ResourceReceiver`/`ResourceSender`
//! state machines: **Resource RX** (advertisement → accept → parts → assemble,
//! hash-verified) and **Resource TX** (leaf-originated), plus a `Store` trait
//! that marks the flash-persistence boundary and a resume path so a transfer
//! survives a dormant link window (T4 sustain).
//!
//! `rns-core` implements the RNS Resource protocol (chunk sequencing, per-chunk
//! hash map, retransmission, completion proof) and is verified against the
//! Python reference — the host tests here prove the *leaf path* drives it, and
//! the cross-language hash vectors lock `compute_resource_hash`/`map_hash` to
//! the Python server's values (design §6b).
//!
//! `no_std` + `alloc`; parts are heap-allocated like rns-core itself.

#![no_std]

extern crate alloc;

pub mod rx;
pub mod store;
pub mod tx;

pub use rx::ResourceRx;
pub use store::Store;
pub use tx::ResourceTx;

/// Default SDU for resource part framing (matches `rns_core::constants`).
pub const RESOURCE_SDU: usize = 464;
