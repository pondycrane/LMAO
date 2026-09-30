//! Offline packet dumper for the `host/lxmf-sim` harness (issue #197).
//!
//! Rebuilds the exact packets the Cardputer firmware puts on the air — the RNS
//! `lxmf.delivery` announce + the opportunistic LXMF DATA packet (with the
//! fixed `encrypt(packed[16..])` wire format) — using the production constants
//! (canonical client identity seed + server delivery/public key, the same ones
//! baked into `firmware/src/rns_link.rs`). The hex it prints feeds Python
//! ingest.py, which simulates the LMAO server accepting them offline.
//!
//! Run + capture:
//!   cargo run -p leaf-lxmf --example sim_dump
//!
//! The client **private** seed here is the already-public firmware constant
//! (a *client* identity; only the server identity's private key is sensitive
//! and never committed — this harness feeds the server's recall with the
//! client's announce, exactly like production).

const NODE_IDENTITY_SEED: [u8; 64] = [
    0x28, 0x0a, 0xea, 0x11, 0xb8, 0x8c, 0x63, 0xb1, 0xc9, 0x1f, 0x8b, 0x9c, 0x73, 0x8c, 0x13, 0x8f,
    0xd4, 0xe8, 0x7e, 0x5d, 0xd5, 0xe5, 0x29, 0xc3, 0xab, 0x98, 0xd6, 0x20, 0x23, 0xbc, 0x9c, 0x75,
    0x31, 0x95, 0xcf, 0xa3, 0xa4, 0x89, 0xa7, 0xf4, 0xa1, 0x7b, 0xa4, 0x12, 0xdf, 0x5b, 0xa5, 0xa9,
    0x0c, 0xde, 0x60, 0xd8, 0xe4, 0xe6, 0x7f, 0xc7, 0xc1, 0x0c, 0xe0, 0x9a, 0xce, 0xd0, 0x42, 0xf8,
];

const SERVER_LXMF_DELIVERY_HASH: [u8; 16] = [
    0xda, 0xd3, 0x5b, 0x80, 0x16, 0x4b, 0x25, 0xf7, 0xb1, 0x47, 0x4b, 0xe8, 0x6e, 0x44, 0x37, 0x02,
];

const SERVER_PUBLIC_KEY: [u8; 64] = [
    0x19, 0x85, 0xac, 0x0e, 0xf9, 0x8f, 0x17, 0xd2, 0x66, 0x71, 0xf2, 0xf9, 0xea, 0x31, 0xc0, 0x59,
    0x3a, 0x90, 0xfe, 0xc1, 0xa3, 0x05, 0x79, 0xfa, 0x68, 0x54, 0x5a, 0x0c, 0xc0, 0x15, 0x94, 0x20,
    0x78, 0x90, 0x18, 0x21, 0x3f, 0x31, 0x15, 0x23, 0x9d, 0x0e, 0xd1, 0x03, 0x6c, 0xf3, 0x37, 0x5e,
    0xe7, 0x5c, 0xd4, 0x10, 0x31, 0x8e, 0xda, 0x87, 0xfb, 0x94, 0x20, 0xce, 0x7a, 0x4c, 0xa7, 0x88,
];

/// Deterministic PRNG — only the validity of the ephemeral X25519 keys matter.
struct XorShift(u64);
impl rns_crypto::Rng for XorShift {
    fn fill_bytes(&mut self, dest: &mut [u8]) {
        for b in dest.iter_mut() {
            self.0 ^= self.0 << 13;
            self.0 ^= self.0 >> 7;
            self.0 ^= self.0 << 17;
            *b = self.0 as u8;
        }
    }
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn main() {
    let identity = rns_crypto::identity::Identity::from_private_key(&NODE_IDENTITY_SEED);
    let ts: f64 = 1_788_000_000.0;
    let node_id = hex(identity.hash());

    let mut rng = XorShift(0x1234_5678_9abc_def0);

    // The production "Hello" wire message (LMAOEnvelope{text}, p:Envelope).
    let envelope = leaf_lxmf::build_text_envelope(
        &node_id,
        "Hello from Rust Cardputer leaf (seq 300)",
        (ts * 1000.0) as u64,
    );
    let msg = leaf_lxmf::build_message_to_server(
        &identity,
        &mut rng,
        &SERVER_LXMF_DELIVERY_HASH,
        &SERVER_PUBLIC_KEY,
        &envelope,
        ts,
    )
    .expect("build+encrypt");
    println!("MSG_PACKET_HEX={}", hex(&msg));

    // Announce for lxmf.delivery with the µReticulum reference random_hash
    // (urandom(5) ‖ unix_time(5, BE)) — the server recalls 99ce32… from this.
    let rh = leaf_lxmf::random_hash([0xde, 0xad, 0xbe, 0xef, 0x01], ts as u64);
    let ann = leaf_lxmf::announce_for(&identity, "lxmf", "delivery", rh).expect("announce");
    println!("ANNOUNCE_HEX={}", hex(&ann));
}
