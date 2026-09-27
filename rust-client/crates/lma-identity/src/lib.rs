//! LMAO leaf identity (design §5 `leaf-rns`, ticket T2).
//!
//! Rust port of the C side's `firmware_common/lma_common/lma_identity.*`:
//! mint / load an RNS [`Identity`], persist the 64-byte private key, and
//! compute the node's OUT `lxmf/delivery` destination DEST — the value a node
//! prints so it can be added to the server's `ALLOWED_CLIENTS`. The DEST is
//! `destination_hash("lxmf", ["delivery"], identity.hash)`, i.e. the RNS
//! rule `sha256(name_hash || identity_hash)[:16]` — **not** the raw identity
//! hash (a subtlety the golden vectors in the tests pin against RNS 1.3.5).
//!
//! Kept `no_std`-friendly at the core (only `alloc`); the storage backend is
//! injected so the host unit tests run on `std` and the firmware can plug an
//! `esp-storage` NVS backend without changing this crate.

#![no_std]
#![cfg_attr(not(test), forbid(unsafe_code))]

extern crate alloc;

use alloc::string::String;
use core::fmt::Write as _;

use rns_core::destination::destination_hash;
use rns_crypto::identity::Identity;

/// The LXMF control destination LMAO uses for its delivery/control messages.
const LXMF_APP: &str = "lxmf";
const DELIVERY_ASPECT: &str = "delivery";

/// Generate a fresh identity.
pub fn mint(rng: &mut dyn rns_crypto::Rng) -> Identity {
    Identity::new(rng)
}

/// Reconstruct an identity from its persisted 64-byte private key.
pub fn load_identity(private_key: &[u8; 64]) -> Identity {
    Identity::from_private_key(private_key)
}

/// The 64-byte private key to persist (X25519 scalar ‖ Ed25519 seed).
pub fn private_key(identity: &Identity) -> [u8; 64] {
    identity.get_private_key().expect("identity is private")
}

/// The node's OUT `lxmf/delivery` destination hash (16 bytes).
pub fn delivery_hash(identity: &Identity) -> [u8; 16] {
    destination_hash(LXMF_APP, &[DELIVERY_ASPECT], Some(identity.hash()))
}

/// Hex-encode the delivery DEST (lowercase; the form logged for ALLOWED_CLIENTS).
pub fn delivery_hash_hex(identity: &Identity) -> String {
    let mut s = String::with_capacity(32);
    for b in delivery_hash(identity) {
        let _ = write!(s, "{:02x}", b);
    }
    s
}

/// Persistence backend for the 64-byte identity private key.
///
/// Host tests use in-memory / file backends; the firmware uses an NVS-backed
/// impl that reuses this exact trait so the identity lifecycle is exercised
/// identically on both sides (T2 accept: "identity survives reset").
pub trait IdentityStore {
    fn load(&mut self) -> Option<[u8; 64]>;
    fn store(&mut self, key: &[u8; 64]);
}

/// Load the persisted identity, or mint + persist a fresh one (first boot).
///
/// Mirrors `lma_identity::load_or_create(ns)` from firmware_common.
pub fn load_or_create(rng: &mut dyn rns_crypto::Rng, store: &mut dyn IdentityStore) -> Identity {
    if let Some(key) = store.load() {
        load_identity(&key)
    } else {
        let identity = mint(rng);
        store.store(&private_key(&identity));
        identity
    }
}
