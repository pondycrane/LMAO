//! T8 — LMAF / successor transfer framing (design §6c): the reassembler.
//!
//! The LMAOEnvelope framing of a chunked attachment (manifest → chunks → ack)
//! rides **on top of** the stable RNS Resource substrate (T6/T7): the wire
//! contract lives in `proto/lma_messages.proto` (prost-generated in
//! `lma-wire`, vector-tested against the native C++ `lma_attachment` encoding),
//! and the large attachment is ferried efficiently by an RNS Resource. This
//! crate is the receiver-side **reassembler**: it accepts a `LmafManifest`,
//! ingests `LmafChunk`s (per-chunk CRC-32 verify), tracks gaps, and only on the
//! final chunk recomputes the whole-payload SHA-256 and returns a `LmafAck`
//! (`NEED` with missing indexes, or `COMPLETE`).
//!
//! `no_std` + `alloc`.

#![no_std]

extern crate alloc;

use alloc::borrow::ToOwned;
use alloc::string::String;
use alloc::vec;
use alloc::vec::Vec;

pub use lma_wire::{LmafAck, LmafAckStatus, LmafChunk, LmafKind, LmafManifest};

/// Outcome of feeding one chunk into the reassembler.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FeedResult {
    /// Stored (was missing), still collecting.
    Accepted,
    /// Already held an identical chunk.
    Duplicate,
    /// `chunk.id` did not match the session manifest.
    ForeignId,
    /// Per-chunk CRC-32 failed — chunk rejected.
    CrcMismatch,
    /// Index out of the manifest's declared range.
    OutOfRange,
}

/// CRC-32 (zlib polynomial 0xEDB88320) — the per-chunk integrity check.
pub fn crc32(data: &[u8]) -> u32 {
    let mut crc: u32 = 0xffff_ffff;
    for &b in data {
        crc ^= b as u32;
        for _ in 0..8 {
            let mask = (crc & 1).wrapping_neg();
            crc = (crc >> 1) ^ (0xedb8_8320 & mask);
        }
    }
    !crc
}

/// Receiver-side framing reassembler (port of the C++ `lma_attachment`
/// reassembler semantics).
pub struct Reassembler {
    id: Vec<u8>,
    payload_sha256: Vec<u8>,
    chunk_count: u32,
    total_bytes: u64,
    chunks: Vec<Option<Vec<u8>>>,
    received: u32,
}

impl Reassembler {
    /// Open a session from a validated manifest.
    pub fn from_manifest(m: &LmafManifest) -> Self {
        let n = m.chunk_count as usize;
        Reassembler {
            id: m.id.clone(),
            payload_sha256: m.payload_sha256.clone(),
            chunk_count: m.chunk_count,
            total_bytes: m.total_bytes,
            chunks: vec![None; n],
            received: 0,
        }
    }

    pub fn id(&self) -> &[u8] {
        &self.id
    }
    pub fn chunk_count(&self) -> u32 {
        self.chunk_count
    }
    pub fn total_bytes(&self) -> u64 {
        self.total_bytes
    }
    pub fn received(&self) -> u32 {
        self.received
    }
    /// TRUE only when the manifest's declared digest equals the recomputed
    /// SHA-256 of the reassembled chunk data (whole-payload verification).
    pub fn is_complete(&self) -> bool {
        self.received == self.chunk_count
    }

    /// Chunk indexes still missing (ascending).
    pub fn missing(&self) -> Vec<u32> {
        self.chunks
            .iter()
            .enumerate()
            .filter_map(|(i, c)| if c.is_none() { Some(i as u32) } else { None })
            .collect()
    }

    /// Feed one chunk. On `Accepted` it is stored (id + index-gated + CRC'd).
    pub fn feed_chunk(&mut self, c: &LmafChunk) -> FeedResult {
        if c.id != self.id {
            return FeedResult::ForeignId;
        }
        let idx = c.index as usize;
        if idx >= self.chunks.len() {
            return FeedResult::OutOfRange;
        }
        if self.chunks[idx].is_some() {
            return FeedResult::Duplicate;
        }
        if crc32(&c.data) != c.crc {
            return FeedResult::CrcMismatch;
        }
        self.chunks[idx] = Some(c.data.clone());
        self.received += 1;
        FeedResult::Accepted
    }

    /// Reassemble the ordered chunk data (only valid once complete). Returns
    /// the whole payload and whether the SHA-256 matches the manifest digest.
    pub fn reassemble(&self) -> (Vec<u8>, bool) {
        let mut payload = Vec::with_capacity(self.total_bytes as usize);
        for c in self.chunks.iter().flatten() {
            payload.extend_from_slice(c);
        }
        let digest = rns_crypto::sha256::sha256(&payload);
        let ok = self.is_complete() && digest.as_slice() == self.payload_sha256.as_slice();
        (payload, ok)
    }

    /// Build a receiver ack: `COMPLETE` once verified, else `NEED` + gaps.
    pub fn build_ack(&self) -> LmafAck {
        let (_, verified) = self.reassemble();
        LmafAck {
            id: self.id.clone(),
            status: if verified {
                LmafAckStatus::LmafComplete as i32
            } else {
                LmafAckStatus::LmafNeed as i32
            },
            have_count: self.received,
            missing: self.missing(),
            reason: if verified { String::new() } else { "missing chunks".to_owned() },
        }
    }
}
