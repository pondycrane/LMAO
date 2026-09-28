//! T8 cross-language equivalence: the prost-generated LMAF/successor framing
//! messages must serialize byte-for-byte to the native C++ client's
//! `lma_attachment` wire (design §6c — the framing contract lives in the
//! `.proto`, generated everywhere, so it cannot drift).
//!
//! Goldens generated from a pure-Python reference encoder that reproduces the
//! pinned C++ `lma_attachment` field layout exactly (`put_field_len`/`varint`/
//! `fixed32`, and **non-packed** repeated `missing`/`kinds`, matching the C++).
//! Envelopes wrap each message in the LMAOEnvelope oneof (fields 40/41/42/43).
//!
//! Transfer id / digest from payload b"leaf chart payload 0123456789".

use lma_wire::lmao_envelope::Payload;
use lma_wire::{LmafAck, LmafAckStatus, LmafCapability, LmafChunk, LmafKind, LmafManifest};
use prost::Message;

const TRANSFER_ID: &str = "f3c0d3938a74767d17da57c2fcadb658"; // sha256(payload)[:16]
const PAYLOAD_SHA256: &str =
    "f3c0d3938a74767d17da57c2fcadb6583d06b91ff5707a3e4f5200f7c6e7f237";
const MANIFEST_GOLDEN: &str = "c202610a10f3c0d3938a74767d17da57c2fcadb6581220f3c0d3938a74767d17da57c2fcadb6583d06b91ff5707a3e4f5200f7c6e7f237180622126c6d616f3a63686172742d6c696e652d763128403002381d6a086c6561662d65326570fbd095ffbc31";
const CHUNK_GOLDEN: &str = "ca02380a10f3c0d3938a74767d17da57c2fcadb65810011a1d6c656166206368617274207061796c6f61642030313233343536373839255de28a6f";
const ACK_GOLDEN: &str = "d202250a10f3c0d3938a74767d17da57c2fcadb65810021801200320072a096e656564206d6f7265";
const CAP_GOLDEN: &str = "da0220080110061a126c6d616f3a63686172742d6c696e652d763120f0012880203004";

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{:02x}", x)).collect()
}

fn hex_to_bytes(h: &str) -> Vec<u8> {
    (0..h.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&h[i..i + 2], 16).unwrap())
        .collect()
}

fn manifest() -> LmafManifest {
    LmafManifest {
        id: hex_to_bytes(TRANSFER_ID),
        payload_sha256: hex_to_bytes(PAYLOAD_SHA256),
        kind: LmafKind::LmafChart as i32,
        codec: "lmao:chart-line-v1".to_owned(),
        chunk_size: 64,
        chunk_count: 2,
        total_bytes: 29,
        sample_rate: 0,
        channels: 0,
        duration_ms: 0,
        width: 0,
        height: 0,
        node_id: "leaf-e2e".to_owned(),
        created_ms: 1700000000123,
    }
}

fn encode_envelope(p: Payload) -> Vec<u8> {
    let env = lma_wire::LmaoEnvelope { payload: Some(p) };
    let mut buf = Vec::new();
    env.encode(&mut buf).unwrap();
    buf
}

#[test]
fn lmaf_manifest_matches_cpp_wire() {
    let m = manifest();
    let buf = encode_envelope(Payload::Manifest(m));
    assert_eq!(hex(&buf), MANIFEST_GOLDEN);
}

#[test]
fn lmaf_chunk_matches_cpp_wire() {
    let c = LmafChunk {
        id: hex_to_bytes(TRANSFER_ID),
        index: 1,
        data: b"leaf chart payload 0123456789"[..29].to_vec(),
        crc: 0x6f8ae25d,
    };
    let buf = encode_envelope(Payload::Chunk(c));
    assert_eq!(hex(&buf), CHUNK_GOLDEN);
}

#[test]
fn lmaf_ack_matches_cpp_wire_nonpacked_missing() {
    let a = LmafAck {
        id: hex_to_bytes(TRANSFER_ID),
        status: LmafAckStatus::LmafNeed as i32,
        have_count: 1,
        missing: vec![3, 7],
        reason: "need more".to_owned(),
    };
    let buf = encode_envelope(Payload::AckLmaf(a));
    assert_eq!(hex(&buf), ACK_GOLDEN);
}

#[test]
fn lmaf_capability_matches_cpp_wire() {
    let c = LmafCapability {
        lmaf_version: 1,
        kinds: vec![LmafKind::LmafChart as i32],
        codecs: vec!["lmao:chart-line-v1".to_owned()],
        max_chunk_size: 240,
        max_attachment_bytes: 4096,
        rx_window: 4,
        airtime_budget_bps: 0,
    };
    let buf = encode_envelope(Payload::Capability(c));
    assert_eq!(hex(&buf), CAP_GOLDEN);
}
