//! LMAO leaf radio framing (design §5 `radio-interface`, ticket T3).
//!
//! Faithful Rust port of the C++ `lma_rnode_framing` from firmware_common /
//! the vendored RTReticulum tree — the RNode LoRa RF frame demux used to talk
//! to the production mesh.
//!
//! Wire format: every LoRa frame carries a **1-byte header** before the
//! payload.
//! - bit 0 (`SPLIT`): a split packet continues into the *next* frame.
//! - upper nibble (`SEQ` mask 0xF0): a tag, kept for logging only.
//!
//! The box-header behaviour below is **measured on air** (see the C++ header's
//! frame log for a server-RNode packet):
//! ```text
//! RX frame 255 B header=0xcb split=1 tag=c0   <- first half, flagged
//! RX frame 102 B header=0x50 split=0 tag=50   <- second half, NOT flagged,
//!                                                 different tag
//! ```
//! So a *flagged frame opens a pair and the very next frame completes it,
//! whatever its header says* — the tag cannot be used to pair halves (RNS's
//! own `isSplitPacket && seq == sequence` rule never assembles these, which is
//! why the reference node used to log `dropping stale split fragment` for every
//! packet addressed to it).
//!
//! Pure logic (`no_std` + `alloc`), host-tested; the SX1262 SPI driver plugs
//! this demux in on the device side.

#![no_std]
#![cfg_attr(not(test), forbid(unsafe_code))]

extern crate alloc;

use alloc::{vec, vec::Vec};

/// bit 0: a split packet whose second half follows in the next frame.
pub const FLAG_SPLIT: u8 = 0x01;
/// upper nibble: the split tag (upper 4 bits of the header byte). Logging only.
pub const SEQ_MASK: u8 = 0xF0;

/// A parsed LoRa frame: header flags + payload (borrowed).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RnodeFrame<'a> {
    /// header bit 0 — the packet continues into the *next* frame.
    pub split: bool,
    /// upper-nibble tag (logging only; not usable for pairing).
    pub seq: u8,
    /// the payload after the 1-byte header.
    pub payload: &'a [u8],
}

/// Parse the 1-byte RNode frame header. A zero-length buffer yields a
/// non-split, empty frame (never panics).
pub fn parse_rnode_frame(buf: &[u8]) -> RnodeFrame<'_> {
    if buf.is_empty() {
        return RnodeFrame {
            split: false,
            seq: 0,
            payload: &[],
        };
    }
    let header = buf[0];
    RnodeFrame {
        split: (header & FLAG_SPLIT) != 0,
        seq: header & SEQ_MASK,
        payload: &buf[1..],
    }
}

/// Reassembles split LoRa frames into whole RNS packets. Mirrors the C++
/// `RnodeSplitAssembler` exactly, including the measured on-air rule that the
/// *next* frame (whatever its header) completes an open pair.
#[derive(Debug)]
pub struct SplitAssembler {
    max_packet: usize,
    timeout_ms: u32,
    pending_active: bool,
    partial: Vec<u8>,
    partial_at_ms: u32,
    completed: u64,
    dropped: u64,
}

/// Outcome of feeding one parsed frame.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct PushResult {
    /// `data` holds a whole RNS packet.
    pub complete: bool,
    /// this push discarded something (oversize split).
    pub dropped: bool,
    /// an aged-out fragment was discarded.
    pub stale: bool,
    pub stale_len: usize,
    pub data: Vec<u8>,
}

impl SplitAssembler {
    pub fn new(max_packet: usize, timeout_ms: u32) -> Self {
        Self {
            max_packet,
            timeout_ms,
            pending_active: false,
            partial: Vec::new(),
            partial_at_ms: 0,
            completed: 0,
            dropped: 0,
        }
    }

    /// Feed one parse-`frame`. `now_ms` is a monotonic millisecond clock.
    pub fn push(&mut self, frame: &RnodeFrame<'_>, now_ms: u32) -> PushResult {
        let mut r = PushResult::default();

        // Retire a fragment whose partner never came, before the lookup so a
        // reused tag starts cleanly rather than appending to a corpse.
        if self.pending_active && now_ms.wrapping_sub(self.partial_at_ms) > self.timeout_ms {
            r.stale = true;
            r.stale_len = self.partial.len();
            self.dropped += 1;
            self.pending_active = false;
            self.partial.clear();
        }

        if self.pending_active {
            // The very next frame is the partner, whatever its header says
            // (measured on air, see module docs).
            if self.partial.len() + frame.payload.len() > self.max_packet {
                r.dropped = true;
                self.dropped += 1;
                self.pending_active = false;
                self.partial.clear();
                return r;
            }
            self.partial.extend_from_slice(frame.payload);
            r.complete = true;
            r.data = core::mem::take(&mut self.partial);
            self.pending_active = false;
            self.completed += 1;
            return r;
        }

        if !frame.split {
            // Complete on its own — the common case (announces, small packets).
            r.complete = true;
            r.data = frame.payload.to_vec();
            self.completed += 1;
            return r;
        }

        // Flagged, nothing pending: hold for the next frame.
        self.partial.extend_from_slice(frame.payload);
        self.partial_at_ms = now_ms;
        self.pending_active = true;
        r
    }

    pub fn clear(&mut self) {
        self.pending_active = false;
        self.partial.clear();
    }

    pub fn completed(&self) -> u64 {
        self.completed
    }
    pub fn dropped(&self) -> u64 {
        self.dropped
    }
    pub fn in_flight(&self) -> bool {
        self.pending_active
    }
}

/// Split a whole packet into RNode LoRa frames for transmit, matching the
/// on-air layout (first half flagged, subsequent halves unflagged).
///
/// Each frame is header-byte ‖ payload. Frames after the first carry no SPLIT
/// flag; the header is always present (an empty payload yields one empty
/// non-split frame).
///
/// 254-B payload frames are split (508 B max), which is how an LMAF manifest or
/// chunk rides a LoRa frame.
pub fn split_into_frames(payload: &[u8]) -> Vec<Vec<u8>> {
    const MAX_FRAME_PAYLOAD: usize = 254;
    const TAG: u8 = 0x00;

    if payload.is_empty() {
        return vec![vec![0x00]];
    }

    let n = payload.len().div_ceil(MAX_FRAME_PAYLOAD);
    let mut frames = Vec::with_capacity(n);
    for (i, chunk) in payload.chunks(MAX_FRAME_PAYLOAD).enumerate() {
        let more = i + 1 < n;
        let mut f = Vec::with_capacity(1 + chunk.len());
        f.push(TAG | if more { FLAG_SPLIT } else { 0x00 });
        f.extend_from_slice(chunk);
        frames.push(f);
    }
    frames
}

/// Fixed LoRa radio parameters for the Cardputer SX1262 (design §4: "RF params
/// fixed: 868/BW125/SF7/CR4:5/pre24/syncword 0x1424 — match the server, do not
/// renegotiate"). Mirrors `cardputer_client/firmware/main/lora_interface.cpp`
/// (TCXO 1.8 V on DIO3, DIO2 as the RF TX/RX switch, CRC **on** — upstream RNode
/// enables CRC, so without it a real RNode silently drops our frames).
///
/// The SX1262 SPI driver (esp-hal) is wired to these constants; the on-device
/// radio loopback that exercises them is T3's hardware leg (pending the flash
/// path — see T3-GATE.md).
pub mod rf_params {
    /// Carrier frequency (Hz).
    pub const FREQ_MHZ: u32 = 868;
    /// Bandwidth (Hz): 125 kHz.
    pub const BW_HZ: u32 = 125_000;
    /// Spreading factor: 7.
    pub const SF: u8 = 7;
    /// Coding rate: 4/5.
    pub const CR: u8 = 5;
    /// Preamble length (symbols): 24.
    pub const PREAMBLE: u16 = 24;
    /// Sync word: 0x1424.
    pub const SYNCWORD: u16 = 0x1424;
    /// TCXO voltage for DIO3 (mV): 1.8 V.
    pub const TCXO_MV: u32 = 1800;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_header_flags_and_payload() {
        let f = parse_rnode_frame(&[0xcb, 0xAA, 0xBB]);
        assert!(f.split);
        assert_eq!(f.seq, 0xC0);
        assert_eq!(f.payload, &[0xAA, 0xBB]);

        let f = parse_rnode_frame(&[0x50, 0x01]);
        assert!(!f.split);
        assert_eq!(f.seq, 0x50);
        assert_eq!(f.payload, &[0x01]);

        let f = parse_rnode_frame(&[]);
        assert!(!f.split);
        assert!(f.payload.is_empty());
    }

    #[test]
    fn non_split_completes_immediately() {
        let mut a = SplitAssembler::new(1024, 500);
        let r = a.push(&parse_rnode_frame(&[0x00, 1, 2, 3]), 0);
        assert!(r.complete);
        assert_eq!(r.data, [1, 2, 3]);
        assert_eq!(a.completed(), 1);
    }

    #[test]
    fn flagged_then_next_frame_completes_regardless_of_header() {
        // Measured on-air example: 0xcb (split, tag c0) then 0x50 (no split,
        // tag 50) — second half carries neither flag nor same tag.
        let mut a = SplitAssembler::new(1024, 500);
        let first = a.push(&parse_rnode_frame(&[0xcb, 0x10, 0x11]), 0);
        assert!(!first.complete);
        assert!(a.in_flight());

        let second = a.push(&parse_rnode_frame(&[0x50, 0x20, 0x21]), 1);
        assert!(second.complete);
        assert_eq!(second.data, [0x10, 0x11, 0x20, 0x21]);
        assert_eq!(a.completed(), 1);
        assert!(!a.in_flight());
    }

    #[test]
    fn stale_fragment_is_retired() {
        let mut a = SplitAssembler::new(1024, 500);
        a.push(&parse_rnode_frame(&[0x01, 0xde, 0xad]), 0);
        // well past the 500 ms timeout
        let r = a.push(&parse_rnode_frame(&[0x00, 0xbe, 0xef]), 1000);
        assert!(r.stale);
        assert_eq!(r.stale_len, 2);
        // the new frame was not treated as a partner; it completed on its own
        assert!(r.complete);
        assert_eq!(r.data, [0xbe, 0xef]);
        assert_eq!(a.dropped(), 1);
    }

    #[test]
    fn oversize_partner_is_dropped() {
        let mut a = SplitAssembler::new(4, 500);
        a.push(&parse_rnode_frame(&[0x01, 1, 2, 3]), 0); // holds 3 bytes
        let r = a.push(&parse_rnode_frame(&[0x00, 9, 9]), 1); // 3+2 > 4
        assert!(r.dropped);
        assert!(!r.complete);
        assert_eq!(a.dropped(), 1);
    }

    #[test]
    fn split_into_frames_matches_wire_layout() {
        // >254 bytes -> first frame flagged, second not (matches on-air 0xcb/0x50).
        let payload: Vec<u8> = (0..300).map(|i| i as u8).collect();
        let frames = split_into_frames(&payload);
        assert_eq!(frames.len(), 2);
        assert!(frames[0][0] & FLAG_SPLIT != 0);
        assert_eq!(frames[1][0] & FLAG_SPLIT, 0);
        // reassemble via the demux (feed the flagged first, then the unflagged
        // second) == the original
        let f0 = parse_rnode_frame(&frames[0]);
        let f1 = parse_rnode_frame(&frames[1]);
        let mut a = SplitAssembler::new(2048, 500);
        let r0 = a.push(&f0, 0);
        assert!(!r0.complete);
        let r1 = a.push(&f1, 1);
        assert!(r1.complete);
        assert_eq!(r1.data, payload);
    }

    #[test]
    fn single_small_frame_roundtrip() {
        let payload = b"hello".to_vec();
        let frames = split_into_frames(&payload);
        assert_eq!(frames.len(), 1);
        assert_eq!(frames[0][0] & FLAG_SPLIT, 0);
        let mut a = SplitAssembler::new(1024, 500);
        let r = a.push(&parse_rnode_frame(&frames[0]), 0);
        assert!(r.complete);
        assert_eq!(r.data, payload);
    }
}
