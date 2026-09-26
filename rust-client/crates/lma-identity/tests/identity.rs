//! T2 host tests: delivery DEST derivation (cross-language goldens vs Python
//! RNS 1.3.5, the production `lmao-server` reference) and identity persistence.
//!
//! The goldens were generated from RNS 1.3.5:
//!   RNS.Identity.from_bytes(K) → Destination(idn, OUT, SINGLE, "lxmf", "delivery").hash
//! and must match the C client's `lma_identity::delivery_hash` (same RNS wire).

use lma_identity::{IdentityStore, load_identity, load_or_create};
use rns_crypto::OsRng;

/// Fixed key #1: bytes 0..=63.
const KEY1: [u8; 64] = [
    0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e,
    0x0f, 0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1a, 0x1b, 0x1c, 0x1d,
    0x1e, 0x1f, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x29, 0x2a, 0x2b, 0x2c,
    0x2d, 0x2e, 0x2f, 0x30, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3a, 0x3b,
    0x3c, 0x3d, 0x3e, 0x3f,
];
/// RNS 1.3.5 delivery DEST for KEY1 (Python golden).
const GOLDEN1: &str = "fae321c442e3c9bdcd7a3e79d850e03c";

/// Key #2: 32×0xAB ‖ 32×0xCD.
fn key2() -> [u8; 64] {
    let mut k = [0u8; 64];
    k[..32].copy_from_slice(&[0xAB; 32]);
    k[32..].copy_from_slice(&[0xCD; 32]);
    k
}
/// RNS 1.3.5 delivery DEST for KEY2 (Python golden).
const GOLDEN2: &str = "ef969713c140949ad3d1af36d64e963c";

#[test]
fn delivery_matches_python_golden_1() {
    let id = load_identity(&KEY1);
    assert_eq!(lma_identity::delivery_hash_hex(&id), GOLDEN1);
}

#[test]
fn delivery_matches_python_golden_2() {
    let id = load_identity(&key2());
    assert_eq!(lma_identity::delivery_hash_hex(&id), GOLDEN2);
}

#[test]
fn delivery_differs_from_raw_identity_hash() {
    // The DEST is sha256(name_hash || identity_hash)[:16], not identity.hash.
    let id = load_identity(&KEY1);
    assert_ne!(
        lma_identity::delivery_hash(&id).as_slice(),
        id.hash().as_slice()
    );
    // Sanity: the plain RNS identity hash also matches Python (golden).
    let mut h = String::new();
    for b in id.hash() {
        use std::fmt::Write;
        let _ = write!(h, "{:02x}", b);
    }
    assert_eq!(h, "aca31af0441d81dbec71e82da0b4b5f5");
}

/// In-memory `IdentityStore` — stands in for the firmware NVS backend.
struct InMem {
    key: Option<[u8; 64]>,
}

impl IdentityStore for InMem {
    fn load(&mut self) -> Option<[u8; 64]> {
        self.key
    }
    fn store(&mut self, k: &[u8; 64]) {
        self.key = Some(*k);
    }
}

#[test]
fn load_or_create_persists_and_identity_survives_reset() {
    // First boot: mint + persist.
    let mut first = InMem { key: None };
    let id1 = load_or_create(&mut OsRng, &mut first);
    let key = first.key.expect("first boot should persist the private key");
    let dest1 = lma_identity::delivery_hash_hex(&id1);
    assert_eq!(lma_identity::private_key(&id1), key);

    // Simulate a reset: a fresh store bound to the persisted bytes must yield
    // the same identity (same DEST) without re-minting.
    let mut after_reset = InMem { key: Some(key) };
    let id2 = load_or_create(&mut OsRng, &mut after_reset);
    assert_eq!(lma_identity::delivery_hash_hex(&id2), dest1);
    assert_eq!(lma_identity::private_key(&id2), key);
}

#[test]
fn mint_produces_distinct_identities() {
    let a = lma_identity::mint(&mut OsRng);
    let b = lma_identity::mint(&mut OsRng);
    assert_ne!(lma_identity::delivery_hash(&a), lma_identity::delivery_hash(&b));
}
