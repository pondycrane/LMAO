//! Cross-language equivalence: the prost-generated LMAO wire types must
//! serialize byte-for-byte to the Python reference encoder
//! (`cardputer_client/proto/lma_encoder.py`) — the design §6b anti-drift lock.
//!
//! Goldens generated from `lma_encoder.py` for:
//!   node_id="testnode", seq=7, battery=3.3f32,
//!   readings=[{sensor_id:1, value:32.5f32, unit:"C", timestamp_ms:1700000000123}].

use lma_wire::{lmao_envelope, reading, rssi_reading, sensor_envelope, LmaoEnvelope, SensorReport};
use prost::Message;

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{:02x}", x)).collect()
}

const REPORT_GOLDEN: &str =
    "0a08746573746e6f646510071d333353402211080115000002421a014320fbd095ffbc31";
const ENVELOPE_GOLDEN: &str =
    "52240a08746573746e6f646510071d333353402211080115000002421a014320fbd095ffbc31";

fn sample_report() -> SensorReport {
    SensorReport {
        node_id: "testnode".to_owned(),
        seq: 7,
        battery: 3.3,
        readings: vec![reading(1, 32.5, "C", 1700000000123)],
    }
}

#[test]
fn sensor_report_serializes_identically_to_python_reference() {
    let mut buf = Vec::new();
    sample_report().encode(&mut buf).unwrap();
    assert_eq!(hex(&buf), REPORT_GOLDEN);
}

#[test]
fn envelope_serializes_identically_to_python_reference() {
    let env = sensor_envelope("testnode", 7, 3.3, vec![reading(1, 32.5, "C", 1700000000123)]);
    let mut buf = Vec::new();
    env.encode(&mut buf).unwrap();
    assert_eq!(hex(&buf), ENVELOPE_GOLDEN);
}

#[test]
fn roundtrip_report_decode_matches() {
    let r = sample_report();
    let mut buf = Vec::new();
    r.encode(&mut buf).unwrap();
    let back = SensorReport::decode(&buf[..]).expect("decodes");
    assert_eq!(back.node_id, "testnode");
    assert_eq!(back.seq, 7);
    assert_eq!(back.battery, 3.3);
    assert_eq!(back.readings.len(), 1);
    assert_eq!(back.readings[0].sensor_id, 1);
    assert_eq!(back.readings[0].unit, "C");
    assert_eq!(back.readings[0].timestamp_ms, 1700000000123);
    // float exactness: 32.5 is exactly representable
    assert_eq!(back.readings[0].value, 32.5);
}

#[test]
fn rssi_reading_matches_python_golden() {
    // sensor_id 9 = RSSI (dBm); a leaf stamps the radio RSSI on each frame (T9).
    let env = sensor_envelope("node1", 1, 3.3, vec![rssi_reading(-75.0, 1700000000123)]);
    let mut buf = Vec::new();
    env.encode(&mut buf).unwrap();
    assert_eq!(
        hex(&buf),
        "52230a056e6f64653110011d333353402213080915000096c21a0364426d20fbd095ffbc31"
    );

    let back = LmaoEnvelope::decode(&buf[..]).unwrap();
    let r = match back.payload {
        Some(lmao_envelope::Payload::Sensor(s)) => s.readings,
        _ => panic!("expected Sensor"),
    };
    assert_eq!(r.len(), 1);
    assert_eq!(r[0].sensor_id, 9);
    assert_eq!(r[0].unit, "dBm");
    assert_eq!(r[0].value, -75.0); // f32 -75.0 exact
}
