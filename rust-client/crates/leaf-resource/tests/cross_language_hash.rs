//! Cross-language equivalence: rns-core's resource hashing must be byte-
//! identical to the Python RNS 1.3.5 server (the T6 accept), so the leaf
//! reassembles and hash-verifies exactly what the gateway encoded.
//!
//! Goldens generated from `RNS.Identity` in RNS 1.3.5 (the T0/T6 interop
//! baseline) for:
//!   data = b"hello leaf resource vectors", random_hash = bytes([1,2,3,4])
//! RNS formula (Resource.py): hash = full_hash(data+random),
//!   map_hash = full_hash(part+random)[:4], expected_proof = full_hash(data+hash).

use rns_core::resource::{compute_expected_proof, compute_resource_hash};
use rns_core::resource::parts::map_hash;

const DATA: &[u8] = b"hello leaf resource vectors";
const RANDOM: [u8; 4] = [1, 2, 3, 4];

#[test]
fn resource_hash_matches_python_rns_135() {
    let h = compute_resource_hash(DATA, &RANDOM);
    assert_eq!(
        hex(&h),
        "2e7edaca516ad6e790a2f268c2068e2a510fa3fb3d99fad5d907dae77fa9623d"
    );
}

#[test]
fn map_hash_matches_python_rns_135() {
    let mh = map_hash(DATA, &RANDOM);
    assert_eq!(hex(&mh), "2e7edaca");
}

#[test]
fn expected_proof_matches_python_rns_135() {
    let rh = compute_resource_hash(DATA, &RANDOM);
    let p = compute_expected_proof(DATA, &rh);
    assert_eq!(
        hex(&p),
        "1eafc6e89680a1658de54ef72ef37b22b67d30a2ea6d4c30f29f3c391b7ab512"
    );
}

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{:02x}", x)).collect()
}
