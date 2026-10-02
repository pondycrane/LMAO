//! no_std protobuf encoder for `LMAOEnvelope{sensor: SensorReport}` (field 10)
//! — byte-exact with `firmware_common/lma_common/lma_encoder.{h,cpp}` (the C++
//! Sprout sender), so the Rust Sprout's sensor bundle is identical on the wire
//! and the Rust server's prost `contacts.rs` decodes it unchanged.
//!
//! Schema (`proto/lma_messages.proto`):
//!   LMAOEnvelope{ SensorReport sensor = 10 }
//!   SensorReport   { string node_id=1; uint32 seq=2; float battery=3;
//!                    repeated SensorReading readings=4 }
//!   SensorReading  { uint32 sensor_id=1; float value=2; string unit=3;
//!                    uint64 timestamp_ms=4 }
//!
//! Wire types: 0=varint, 2=length-delimited, 5=fixed32 (LE float).

extern crate alloc;
use alloc::vec::Vec;

fn varint(out: &mut Vec<u8>, mut v: u64) {
    while v >= 0x80 {
        out.push(((v & 0x7f) | 0x80) as u8);
        v >>= 7;
    }
    out.push(v as u8);
}

fn field_varint(out: &mut Vec<u8>, field: u32, v: u64) {
    varint(out, ((field as u64) << 3) | 0);
    varint(out, v);
}

fn field_len(out: &mut Vec<u8>, field: u32, payload: &[u8]) {
    varint(out, ((field as u64) << 3) | 2);
    varint(out, payload.len() as u64);
    out.extend_from_slice(payload);
}

fn field_fixed32(out: &mut Vec<u8>, field: u32, f: f32) {
    varint(out, ((field as u64) << 3) | 5);
    out.extend_from_slice(&f.to_le_bytes());
}

/// One `SensorReading` (sensor_id per the shared registry in the proto).
pub fn encode_reading(sensor_id: u32, value: f32, unit: &str, timestamp_ms: u64) -> Vec<u8> {
    let mut r = Vec::new();
    field_varint(&mut r, 1, sensor_id as u64);
    field_fixed32(&mut r, 2, value);
    field_len(&mut r, 3, unit.as_bytes());
    field_varint(&mut r, 4, timestamp_ms);
    r
}

/// `SensorReport` bundling several pre-encoded readings.
pub fn encode_sensor_report(
    node_id: &str,
    seq: u32,
    battery: f32,
    readings: &[Vec<u8>],
) -> Vec<u8> {
    let mut r = Vec::new();
    field_len(&mut r, 1, node_id.as_bytes());
    field_varint(&mut r, 2, seq as u64);
    field_fixed32(&mut r, 3, battery);
    for rd in readings {
        field_len(&mut r, 4, rd);
    }
    r
}

/// `LMAOEnvelope.sensor = 10` wrapper.
pub fn encode_envelope(sensor_report: &[u8]) -> Vec<u8> {
    let mut e = Vec::new();
    field_len(&mut e, 10, sensor_report);
    e
}

#[cfg(test)]
mod tests {
    use super::*;
    use alloc::format;
    use alloc::string::String;

    fn hex(b: &[u8]) -> String {
        b.iter().map(|c| format!("{c:02x}")).collect()
    }

    // Golden vectors from smart_irrigation/native-client/tests/
    // lma_encoder_test.cpp (+ cardputer_client/proto/lma_encoder.py).
    #[test]
    fn reading_golden() {
        assert_eq!(hex(&encode_reading(3, 25.5, "C", 123456)), "0803150000cc411a014320c0c407");
        assert_eq!(hex(&encode_reading(2, 59.0, "%", 123457)), "08021500006c421a012520c1c407");
        // Soil moisture (id 4, %).
        assert_eq!(hex(&encode_reading(4, 35.0, "%", 123458)), "08041500000c421a012520c2c407");
    }

    #[test]
    fn sensor_report_golden() {
        let rd_t = encode_reading(3, 25.5, "C", 123456);
        let rd_h = encode_reading(2, 59.0, "%", 123457);
        let rep = encode_sensor_report("a1b2c3", 7, 3.7, &[rd_t, rd_h]);
        assert_eq!(
            hex(&rep),
            "0a0661316232633310071dcdcc6c40220e0803150000cc411a014320c0c407\
             220e08021500006c421a012520c1c407"
        );
    }

    #[test]
    fn envelope_golden() {
        let rd_t = encode_reading(3, 25.5, "C", 123456);
        let rd_h = encode_reading(2, 59.0, "%", 123457);
        let rd_m = encode_reading(4, 35.0, "%", 123458);
        let rep = encode_sensor_report("a1b2c3", 7, 3.7, &[rd_t, rd_h, rd_m]);
        let env = encode_envelope(&rep);
        assert_eq!(
            hex(&env),
            "523f0a0661316232633310071dcdcc6c40220e0803150000cc411a014320c0c407\
             220e08021500006c421a012520c1c407\
             220e08041500000c421a012520c2c407"
        );
    }
}
