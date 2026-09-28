//! LMAF/successor framing: manifest → chunks → whole-payload verify → ack.

use lma_framing::{FeedResult, Reassembler, crc32};
use lma_wire::{LmafAckStatus, LmafChunk, LmafKind, LmafManifest};
use rns_crypto::sha256::sha256;

const PAYLOAD: &[u8] = b"the irrigation chart: soil moisture + pump duty over 24h, rendered server-side";
const CHUNK_SIZE: u32 = 16;

fn manifest() -> LmafManifest {
    let digest = sha256(PAYLOAD);
    LmafManifest {
        id: digest[..16].to_vec(),
        payload_sha256: digest.to_vec(),
        kind: LmafKind::LmafChart as i32,
        codec: "lmao:chart-line-v1".to_owned(),
        chunk_size: CHUNK_SIZE,
        chunk_count: PAYLOAD.len().div_ceil(CHUNK_SIZE as usize) as u32,
        total_bytes: PAYLOAD.len() as u64,
        sample_rate: 0,
        channels: 0,
        duration_ms: 0,
        width: 640,
        height: 400,
        node_id: "leaf-e2e".to_owned(),
        created_ms: 1700000000456,
    }
}

fn chunk_at(index: u32) -> LmafChunk {
    let start = (index * CHUNK_SIZE) as usize;
    let end = core::cmp::min(start + CHUNK_SIZE as usize, PAYLOAD.len());
    let data = PAYLOAD[start..end].to_vec();
    let crc = crc32(&data);
    LmafChunk {
        id: manifest().id.clone(),
        index,
        data,
        crc,
    }
}

#[test]
fn crc32_known_vector() {
    // CRC-32 of "123456789" is 0xCBF43926 (the poly0xEDB88320 check value).
    assert_eq!(crc32(b"123456789"), 0xCBF43926);
}

#[test]
fn feed_all_chunks_verifies_and_acks_complete() {
    let n = manifest().chunk_count;
    assert!(n > 1, "expected a multi-chunk transfer");
    let mut r = Reassembler::from_manifest(&manifest());

    for i in 0..n {
        assert_eq!(r.feed_chunk(&chunk_at(i)), FeedResult::Accepted, "chunk {i}");
    }
    assert!(r.is_complete(), "all chunks held");
    assert!(r.missing().is_empty());

    let (payload, verified) = r.reassemble();
    assert!(verified, "whole-payload sha256 must match the manifest digest");
    assert_eq!(payload, PAYLOAD);

    let ack = r.build_ack();
    assert_eq!(ack.status, LmafAckStatus::LmafComplete as i32);
    assert!(ack.missing.is_empty());
}

#[test]
fn missing_chunks_report_need_with_gaps() {
    let mut r = Reassembler::from_manifest(&manifest());
    // Feed only even-indexed chunks.
    let n = manifest().chunk_count;
    for i in (0..n).step_by(2) {
        assert_eq!(r.feed_chunk(&chunk_at(i)), FeedResult::Accepted);
    }
    assert!(!r.is_complete());

    let ack = r.build_ack();
    assert_eq!(ack.status, LmafAckStatus::LmafNeed as i32);
    assert_eq!(ack.have_count, n.div_ceil(2));
    assert_eq!(ack.missing, (0..n).filter(|i| i % 2 == 1).collect::<Vec<_>>());
}

#[test]
fn crc_mismatch_rejects_chunk() {
    let mut r = Reassembler::from_manifest(&manifest());
    let mut c = chunk_at(0);
    c.data[0] ^= 0xff; // corrupt the chunk payload
    assert_eq!(r.feed_chunk(&c), FeedResult::CrcMismatch);
}

#[test]
fn foreign_id_rejected_and_duplicate_ignored() {
    let mut r = Reassembler::from_manifest(&manifest());
    let mut c = chunk_at(0);
    c.id[0] ^= 0x01; // wrong session id
    assert_eq!(r.feed_chunk(&c), FeedResult::ForeignId);

    let d = chunk_at(0);
    assert_eq!(r.feed_chunk(&d), FeedResult::Accepted);
    assert_eq!(r.feed_chunk(&d), FeedResult::Duplicate, "same chunk held once");
    assert_eq!(r.received(), 1);
}
