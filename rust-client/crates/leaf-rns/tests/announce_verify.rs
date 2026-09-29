//! Host verification of the firmware's RNS announce construction.
//!
//! The firmware (`firmware/src/rns_link.rs::build_announce`) builds a signed
//! RNS announce for the node identity / `lmao.leaf` destination. This test
//! reproduces that exact construction from the same identity seed and checks
//! the two things a receiving node (e.g. the sprout) validates:
//!   1. `dest_hash == truncated(sha256(name_hash ‖ identity_hash))`
//!   2. `identity.verify(signature, dest_hash ‖ pubkey ‖ name_hash ‖ random_hash)`

use rns_core::announce::AnnounceData;
use rns_core::destination;
use rns_core::packet::{PacketFlags, RawPacket};

// The same fixed seed the firmware uses (see firmware/src/rns_link.rs).
const NODE_IDENTITY_SEED: [u8; 64] = [
    0x1f, 0x8a, 0x4c, 0xd2, 0x77, 0x9e, 0x3b, 0x51, 0x06, 0x2f, 0x94, 0xbe, 0x60, 0xc5, 0x11, 0x9d,
    0x5e, 0x24, 0x7a, 0x09, 0xb3, 0x46, 0x8f, 0xd1, 0xe2, 0x38, 0x03, 0xfc, 0x0a, 0x8b, 0x66, 0x14,
    0xe9, 0x31, 0x90, 0x2c, 0x6f, 0x5b, 0xd8, 0x47, 0x52, 0x7e, 0x0d, 0xa4, 0xc9, 0x1a, 0x57, 0x90,
    0x66, 0x7b, 0x85, 0x34, 0xfb, 0x16, 0x0e, 0x92, 0xd3, 0x6a, 0xe0, 0x49, 0x3c, 0xc1, 0x79, 0x2b,
];

const APP_NAME: &str = "lmao";
const LEAF_ASPECT: &str = "leaf";

#[test]
fn announce_dest_hash_and_signature_validate_like_a_recipient() {
    let identity = rns_crypto::identity::Identity::from_private_key(&NODE_IDENTITY_SEED);
    let aspects = [LEAF_ASPECT];

    let name_hash = destination::name_hash(APP_NAME, &aspects);
    let dest_hash = destination::destination_hash(APP_NAME, &aspects, Some(identity.hash()));

    // Recipient check 1: reconstructions of the announce destination must match.
    let mut addr_material = Vec::new();
    addr_material.extend_from_slice(&name_hash);
    addr_material.extend_from_slice(identity.hash());
    let recomputed = rns_core::hash::truncated_hash(&addr_material);
    assert_eq!(dest_hash, recomputed);
    assert_ne!(dest_hash, [0u8; 16], "identity-derived dest must not be the old bootstrap");

    // Fresh random_hash (deterministic for the test; the firmware randomizes it).
    let random_hash = [0xAA; 10];

    let (announce_data, has_ratchet) =
        AnnounceData::pack(&identity, &dest_hash, &name_hash, &random_hash, None, None).unwrap();
    assert!(!has_ratchet);

    // The announced identity public key is embedded in the announce data.
    let pubkey = identity.get_public_key().unwrap();
    assert!(announce_data.starts_with(&pubkey), "announce carries the public key first");

    // Recipient check 2 (the sprout's core check): the signature must verify
    // over dest_hash ‖ pubkey ‖ name_hash ‖ random_hash.
    let sig_off = pubkey.len() + name_hash.len() + random_hash.len();
    let signature: [u8; 64] = announce_data[sig_off..sig_off + 64].try_into().unwrap();

    let mut signed_data = Vec::new();
    signed_data.extend_from_slice(&dest_hash);
    signed_data.extend_from_slice(&pubkey);
    signed_data.extend_from_slice(&name_hash);
    signed_data.extend_from_slice(&random_hash);
    assert!(identity.verify(&signature, &signed_data), "announce signature must verify");

    // The wire announce packet is one RNode frame and matches the urns layout:
    // flags(announce, HDR1, single, broadcast) + hops + dest_hash + ctx + data.
    let flags = PacketFlags {
        header_type: 0,
        context_flag: 0,
        transport_type: 0,
        destination_type: rns_core::constants::DESTINATION_SINGLE,
        packet_type: rns_core::constants::PACKET_TYPE_ANNOUNCE,
    };
    let pkt = RawPacket::pack(flags, 0, &dest_hash, None, 0, &announce_data).unwrap();
    assert_eq!(pkt.raw[0] & 0b11, 0b01, "packet type = ANNOUNCE");
    assert_eq!(&pkt.raw[2..18], &dest_hash, "header carries the announce destination");
    assert_eq!(pkt.raw.len(), announce_data.len() + 19);
    assert!(pkt.raw.len() < 255, "fits a single LoRa frame (< 254 payload)");
}
