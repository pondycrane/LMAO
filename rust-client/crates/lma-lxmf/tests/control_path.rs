//! T5 LXMF control-path host test: pack a protobuf SensorReport envelope into a
//! signed LXMF message, unpack + verify, decode back to the same report, and
//! reject tampering. Ed25519 identity keys come from a deterministic FixedRng,
//! so the round-trip is stable.

use lma_identity::{delivery_hash, mint};
use lma_lxmf::{pack_envelope, unpack_envelope_verified, ENVELOPE_TITLE};
use lma_wire::{lmao_envelope, reading, sensor_envelope};
use prost::Message;
use rns_crypto::{identity::Identity, FixedRng};

fn fixed_identity(seed: &[u8]) -> Identity {
    let mut rng = FixedRng::new(seed);
    mint(&mut rng)
}

#[test]
fn pack_sign_unpack_verify_roundtrip() {
    let source = fixed_identity(b"leaf-source-key-seed-0001");
    let server = fixed_identity(b"server-dest-key-seed-0001");
    let dest_hash = delivery_hash(&server); // server's lxmf/delivery DEST

    let envelope =
        sensor_envelope("node1", 1, 3.3, vec![reading(1, 25.0, "C", 1700000000000)]);

    let (packed, message_hash) = pack_envelope(&source, &dest_hash, 1700000000.0, &envelope)
        .expect("packs");

    // Shape: dest(16) + src(16) + sig(64) + msgpack payload
    assert!(packed.len() > 16 + 16 + 64);

    let (decoded, res) = match unpack_envelope_verified(&packed, &source) {
        Ok(v) => v,
        Err(e) => panic!("unpack+verify failed: {:?}", e),
    };
    assert_eq!(res.signature_valid, Some(true));
    assert_eq!(res.title, ENVELOPE_TITLE);
    assert_eq!(res.destination_hash, dest_hash);
    assert_eq!(res.source_hash, *source.hash());
    assert_eq!(res.message_hash, message_hash);

    // Content decodes to the exact SensorReport we packed.
    let report = match decoded.payload.as_ref() {
        Some(lmao_envelope::Payload::Sensor(r)) => r,
        _ => panic!("expected Sensor variant"),
    };
    assert_eq!(report.node_id, "node1");
    assert_eq!(report.seq, 1);
    assert_eq!(report.battery, 3.3);
    assert_eq!(report.readings.len(), 1);
    assert_eq!(report.readings[0].sensor_id, 1);
    assert_eq!(report.readings[0].unit, "C");
    assert_eq!(report.readings[0].timestamp_ms, 1700000000000);

    // The packed envelope re-encodes identically to the Python lma_encoder goldens.
    let mut rebuf = Vec::new();
    decoded.encode(&mut rebuf).unwrap();
    assert_eq!(
        rebuf,
        [
            0x52, 0x21, // field 10 (LMAOEnvelope.sensor), len 0x21=33
            0x0a, 0x05, 0x6e, 0x6f, 0x64, 0x65, 0x31, // node_id="node1"
            0x10, 0x01, // seq=1
            0x1d, 0x33, 0x33, 0x53, 0x40, // battery=3.3 (32-bit LE)
            0x22, 0x11, // reading len 0x11=17
            0x08, 0x01, // sensor_id=1
            0x15, 0x00, 0x00, 0xc8, 0x41, // value=25.0
            0x1a, 0x01, 0x43, // unit="C"
            0x20, 0x80, 0xd0, 0x95, 0xff, 0xbc, 0x31 // timestamp_ms=1700000000000
        ][..]
    );
}

#[test]
fn tampered_content_rejects_signature() {
    let source = fixed_identity(b"leaf-source-key-seed-0002");
    let server = fixed_identity(b"server-dest-key-seed-0002");
    let dest_hash = delivery_hash(&server);
    let envelope =
        sensor_envelope("node1", 1, 3.3, vec![reading(1, 25.0, "C", 1700000000000)]);

    let (mut packed, _) = pack_envelope(&source, &dest_hash, 1700000000.0, &envelope).unwrap();

    // Flip one content byte inside the msgpack payload.
    let sig_len = 16 + 16 + 64;
    packed[sig_len + 20] ^= 0x01;

    // Signature no longer verifies -> rejected by unpack_envelope_verified.
    let err = match unpack_envelope_verified(&packed, &source) {
        Err(e) => e,
        Ok(_) => panic!("expected SignError on tampered content"),
    };
    assert!(matches!(err, lxmf_core::message::Error::SignError));
}
