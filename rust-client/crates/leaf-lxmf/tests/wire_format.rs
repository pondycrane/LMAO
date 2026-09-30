//! Host regression tests pinning the LXMF opportunistic wire contract
//! (issue #197 L1 outbound + the inbound mirror rule):
//!
//! 1. RNS payload = `encrypt(packed[16..])` — the leading 16-byte destination
//!    hash must NOT be inside the encrypted payload.
//! 2. `dest_hash ‖ decrypt(payload)` unpacks + verifies via `lxmf_core`.
//! 3. `random_hash` follows the `urandom(5) ‖ unix_time(5, BE)` reference.
//! 4. The server-reply mirror decode path parses `ACK …\nDATA …`.

use leaf_lxmf::{
    announce_for, build_message_to_server, build_text_envelope, decrypt_server_reply,
    lxmf_delivery_hash, pack_lxmf_to_server, parse_data_line, random_hash, ChartRecord,
};
use rns_crypto::identity::Identity;

// Any two distinct keypairs; the test only needs self-consistent signing +
// encryption on each side, so the 64-byte private-key seeds don't need to
// correspond to production (they're fabricated here).
const CLIENT_SEED: [u8; 64] = [0x11; 64];
const SERVER_SEED: [u8; 64] = [0x22; 64];

/// Deterministic PRNG — only the *validity* of the ephemeral X25519 keys
/// matters, not their specific values.
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

const TS: f64 = 1_788_000_000.0;

/// Unpack `wire` (full `dest‖src‖sig‖payload` message) verifying the signature
/// against `verifier_pubkey` (the key of whoever signed the message).
fn unpack_verified(wire: &[u8], verifier_pubkey: &[u8; 64]) -> lxmf_core::message::UnpackResult {
    let verifier = Identity::from_public_key(verifier_pubkey);
    let verify = |_src: &[u8; 16], sig: &[u8; 64], data: &[u8]| verifier.verify(sig, data);
    lxmf_core::message::unpack(wire, Some(&verify)).expect("unpack of wire message")
}

/// The server signs a reply addressed to `client_dest`, encrypts the
/// opportunistic slice (`packed[16..]`) to the client's public key — exactly
/// the production server→client reply shape.
fn pack_server_reply_to_client(
    server: &Identity,
    client_pubkey: &[u8; 64],
    client_dest: &[u8; 16],
    content: &[u8],
    rng: &mut dyn rns_crypto::Rng,
) -> (Vec<u8>, Vec<u8>) {
    let src_hash = lxmf_delivery_hash(server);
    let packed = lxmf_core::message::pack(
        client_dest,
        &src_hash,
        TS,
        b"p:Envelope",
        content,
        Vec::new(),
        None,
        |data| server.sign(data).map_err(|_| lxmf_core::message::Error::SignError),
    )
    .unwrap()
    .packed;
    // Encrypt the opportunistic slice (no leading dest) to the client.
    let to_client = Identity::from_public_key(client_pubkey);
    let ciphertext = to_client.encrypt(&packed[16..], rng).unwrap();
    (packed, ciphertext)
}

#[test]
fn random_hash_reference_layout() {
    let random5 = [0xde, 0xad, 0xbe, 0xef, 0x01];
    // 1_788_000_000 = 0x6A_92_B7_00 → 64-bit BE = [00 00 00 00 6A 92 B7 00];
    // Python's `struct.pack(">Q", t)[3:]` gives bytes [3..] = [00 6A 92 B7 00].
    let rh = random_hash(random5, 1_788_000_000);
    assert_eq!(&rh[..5], &random5, "first 5 bytes are the sender-fresh random");
    assert_eq!(&rh[5..], &[0x00, 0x6a, 0x92, 0xb7, 0x00], "last 5 = unix_time BE bytes[3..]");
}

#[test]
fn outbound_encrypts_opportunistic_slice() {
    let client = Identity::from_private_key(&CLIENT_SEED);
    let client_pub = client.get_public_key().unwrap();
    let server = Identity::from_private_key(&SERVER_SEED);
    let server_pub = server.get_public_key().unwrap();
    let server_dest = lxmf_delivery_hash(&server);

    let content = b"LMAOEnvelope{text: Hello}";
    let mut rng = XorShift(0x1234_5678_9abc_def0);
    let env = pack_lxmf_to_server(
        &client, &mut rng, &server_dest, &server_pub, b"p:Envelope", content, TS,
    )
    .expect("pack+encrypt");

    // Invariant: the encrypted RNS payload is `packed[16..]`, not the whole
    // message — the wire contract that fixes the server's unpack misalignment.
    let decrypted = server.decrypt(&env.ciphertext).expect("server can decrypt");
    assert_ne!(
        &decrypted[..],
        &env.packed[..],
        "decrypted payload must NOT be the full packed message"
    );
    assert_eq!(
        &decrypted[..],
        &env.packed[16..],
        "RNS payload == encrypt(packed[16..]) — no leading destination hash"
    );

    // Re-prepending the dest hash recovers the exact wire message, which
    // unpacks + verifies (the client signed it) via lxmf-core.
    let mut wire = Vec::new();
    wire.extend_from_slice(&server_dest);
    wire.extend_from_slice(&decrypted);
    assert_eq!(&wire, &env.packed, "dest_hash ‖ decrypt(payload) == packed");

    let res = unpack_verified(&wire, &client_pub);
    assert!(res.signature_valid.unwrap_or(false), "signature verifies against the client key");
    assert_eq!(res.title, b"p:Envelope");
    assert_eq!(res.content, content);
    assert_eq!(res.destination_hash, server_dest, "message addressed to the server");
    assert_eq!(res.source_hash, lxmf_delivery_hash(&client), "source = client delivery hash");
}

#[test]
fn outbound_full_packet_round_trips() {
    let client = Identity::from_private_key(&CLIENT_SEED);
    let client_pub = client.get_public_key().unwrap();
    let server = Identity::from_private_key(&SERVER_SEED);
    let server_pub = server.get_public_key().unwrap();
    let server_dest = lxmf_delivery_hash(&server);

    let mut rng = XorShift(0x0bad_cafe_f00d);
    let pkt = build_message_to_server(
        &client, &mut rng, &server_dest, &server_pub, b"Hello from Cardputer", TS,
    )
    .expect("build raw DATA packet");
    assert!(!pkt.is_empty());

    // The raw packet must be a valid HEADER_1 single-destination DATA frame.
    let parsed = rns_core::packet::RawPacket::unpack(&pkt).expect("parses as an RNS packet");
    assert_eq!(
        parsed.flags.packet_type,
        rns_core::constants::PACKET_TYPE_DATA,
        "wire type is DATA"
    );
    let decrypted = server.decrypt(&parsed.data).expect("server decrypts the wire payload");
    let mut wire = Vec::new();
    wire.extend_from_slice(&server_dest);
    wire.extend_from_slice(&decrypted);
    let res = unpack_verified(&wire, &client_pub);
    assert!(res.signature_valid.unwrap_or(false));
    assert_eq!(res.content, b"Hello from Cardputer");
}

#[test]
fn inbound_reply_mirror_rule() {
    let client = Identity::from_private_key(&CLIENT_SEED);
    let client_pub = client.get_public_key().unwrap();
    let server = Identity::from_private_key(&SERVER_SEED);
    let server_pub = server.get_public_key().unwrap();
    let server_dest = lxmf_delivery_hash(&server);
    let client_delivery = lxmf_delivery_hash(&client);

    // Server replies with `ACK …\nDATA …` addressed to the client's delivery.
    // The reply content is an `LMAOEnvelope{text: TextMessage}` protobuf whose
    // field 2 carries the ACK + positional `DATA node dry wet ct temp… ch
    // humidity… cm samples… [mask]` line (the repo's sprout_history.data_line).
    let node_id_hex = hex_of(client.hash());
    let reply_body = concat!(
        "reply: ACK from LMAO Server \u{2014} received your message (86 bytes)\n",
        "DATA abcdef0123456789 12 34 3 21.5 22.0 20.0 3 45 50 55 2 100 90 5",
    );
    let envelope = build_text_envelope(&node_id_hex, reply_body, (TS * 1000.0) as u64);
    let mut rng = XorShift(0x5555_aaaa);
    let (_packed, ciphertext) =
        pack_server_reply_to_client(&server, &client_pub, &client_delivery, &envelope, &mut rng);

    let reply = decrypt_server_reply(&client, &ciphertext, &server_pub, &client_delivery)
        .expect("client decrypts + parses the reply");
    assert!(reply.sig_valid, "signature verified against the server key");
    assert_eq!(reply.src_hash, server_dest, "reply source = server delivery hash");
    assert_eq!(reply.text, reply_body, "full decoded text (ACK + DATA line)");

    let chart = reply.chart.expect("chart line parsed");
    assert_eq!(
        chart,
        ChartRecord {
            node: "abcdef0123456789".into(),
            dry: 12,
            wet: 34,
            temp: vec![21.5, 22.0, 20.0],
            humidity: vec![45, 50, 55],
            samples: vec![100, 90],
            water_mask: 5,
        }
    );
}

fn hex_of(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push_str(&format!("{b:02x}"));
    }
    s
}

#[test]
fn parse_data_line_tolerates_malformed_and_missing_mask() {
    // Lines without the trailing watering mask are accepted (mask defaults 0).
    let old = parse_data_line("DATA node123 1 2 1 19.5 1 40 0");
    assert_eq!(old.unwrap().water_mask, 0);
    // Malformed/non-DATA lines are skipped without failing the whole message.
    let mixed = parse_data_line(
        "reply: ACK \u{2014} received your message\nnot data\nDATA n 1 2 1 1 1 1 0\n",
    );
    assert!(mixed.is_some(), "scans past a non-DATA line to a valid record");
}

#[test]
fn announce_round_trips_wire_format() {
    let client = Identity::from_private_key(&CLIENT_SEED);
    let rh = random_hash([1, 2, 3, 4, 5], 1_788_000_000);
    let ann = announce_for(&client, "lxmf", "delivery", rh).expect("announce builds");
    let parsed = rns_core::packet::RawPacket::unpack(&ann).expect("parses as an RNS packet");
    assert_eq!(
        parsed.flags.packet_type,
        rns_core::constants::PACKET_TYPE_ANNOUNCE
    );
    assert_eq!(parsed.destination_hash, lxmf_delivery_hash(&client));
}
