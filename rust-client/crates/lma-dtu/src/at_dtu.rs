//! RAK3172 DTU (LoRa P2P) over UART AT — the Sprout node's radio leg, a Rust
//! port of the native-client's `uart_at_interface.{cpp,h}`.
//!
//! Wire format at the RNS boundary is identical to the server RNode / urns
//! `DtuInterface`: every on-air LoRa frame carries a **1-byte RNode header**
//! (upper-nibble seq + bit0 SPLIT flag) BEFORE the RNS packet bytes.  TX goes
//! out as `AT+PSEND=<hex>`, RX arrives as `+EVT:RXP2P:<rssi>:<snr>:<hex>` on
//! UART lines.  Frames ≤ 254 B fit one `AT+PSEND`; larger packets split into
//! two frames (both flagged `seq|0x01`), mirroring the native-client exactly.
//!
//! This module is pure protocol logic (no UART driver) so it is host-testable;
//! `main.rs` drives it over an esp-hal UART.  Reuses the shared
//! `radio-interface` RNode framing demux/split reassembly.

#![no_std]
extern crate alloc;

use alloc::string::{String, ToString};
use alloc::vec;
use alloc::vec::Vec;
use radio_interface::{parse_rnode_frame, SplitAssembler};

/// Atom Lite G22 → RAK3172 RX (UART2 TX).
pub const PIN_UART_TX: u32 = 22;
/// Atom Lite G19 ← RAK3172 TX (UART2 RX).
pub const PIN_UART_RX: u32 = 19;
/// DTU UART baud (the RAK3172 boots at 115200 8N1).
pub const DTU_BAUD: u32 = 115200;

/// Longest payload one `AT+PSEND` accepts (bigger frames are rejected with
/// AT_PARAM_ERROR — measured live on the bench RAK3172: 156 B is already
/// refused, so a single-frame packet is capped at 128 B and anything larger
/// splits into 2 split-flagged frames (the RNode's SplitAssembler reassembles
/// by seq). The native firmware assumed 254 B, which silently fails here.
pub const DTU_FRAME_PAYLOAD: usize = 128;

/// LMAO mesh radio params as RUI4 P2P AT values (must match the server RNode):
/// 868 MHz / BW 125k / SF 7 / CR 4:5 / preamble 24 / syncword 0x1424, and a
/// **max TX power (22 dBm)** — the RAK's power-on default is lower than the
/// Cardputer's 14 dBm SX1262, which is why the Sprout never reached the
/// server's RNode half a rack away while the Cardputer does.
///
/// `PTP` is the P2P peer address (all devices share the same value on the
/// LMAO channel); the RNode + Cardputer ignore the byte-wise address and just
/// demod the frames.
pub const DTU_CONFIG_CMDS: [&str; 7] = [
    "AT+PFREQ=868000000",
    "AT+PSF=7",
    "AT+PBW=0",
    "AT+PCR=0",
    "AT+PPL=24",
    "AT+SYNCWORD=1424",
    "AT+PTP=17",
];

/// The full config boot sequence, in order (each without CRLF; main adds it):
/// 1. `AT+PRECV=0` first — the RAK3172 powers up in P2P RX and *rejects*
///    radio-set commands with `AT_BUSY_ERROR` until RX is disabled;
/// 2. the seven radio-set commands;
/// 3. re-arm continuous RX.
pub fn boot_lines() -> Vec<String> {
    let mut s = vec!["AT+PRECV=0".to_string()];
    for c in DTU_CONFIG_CMDS {
        s.push(c.to_string());
    }
    s.push("AT+PRECV=65535".to_string());
    s
}

/// One UART line to write while transmitting: the raw AT line (main appends
/// `\r\n`) plus how long to wait before reading/parsing its response.
pub type TxLine = (String, u32);

/// Lowercase hex encode.
pub fn to_hex(b: &[u8]) -> String {
    let mut s = String::with_capacity(b.len() * 2);
    for &x in b {
        s.push(char::from_digit((x >> 4) as u32, 16).unwrap_or('0'));
        s.push(char::from_digit((x & 0x0f) as u32, 16).unwrap_or('0'));
    }
    s
}

/// Hex decode (odd length → None).
pub fn from_hex(h: &str) -> Option<Vec<u8>> {
    if h.len() % 2 != 0 {
        return None;
    }
    let mut out = Vec::with_capacity(h.len() / 2);
    let nib = |c: u8| match c {
        b'0'..=b'9' => Some(c - b'0'),
        b'a'..=b'f' => Some(c - b'a' + 10),
        b'A'..=b'F' => Some(c - b'A' + 10),
        _ => None,
    };
    for pair in h.as_bytes().chunks_exact(2) {
        out.push((nib(pair[0])? << 4) | nib(pair[1])?);
    }
    Some(out)
}

/// The hex payload of a `+EVT:RXP2P` event line — the RUI4 firmware appends
/// `:<rssi>:<snr>:<hexpayload>` after the event tag, so the payload is after
/// the *last* colon on the line.
pub fn event_hex(line: &str) -> Option<&str> {
    line.rfind(':').and_then(|c| line.get(c + 1..))
}

/// Convert a whole RNS packet into the ordered AT lines needed to TX it on
/// air.  Per frame: RX off → `AT+PSEND=<hex>` (the RNode header + payload) →
/// RX re-armed.  `seq` is the upper-nibble tag (any 4-bit value; random is
/// fine).  Packets > 254 B split into two frames, both flagged `seq|0x01`.
pub fn tx_lines(packet: &[u8], seq: u8) -> Vec<TxLine> {
    let mut out = Vec::new();
    let split = packet.len() > DTU_FRAME_PAYLOAD;
    let seq = seq & 0xF0;
    let mut off = 0usize;
    loop {
        let chunk = core::cmp::min(DTU_FRAME_PAYLOAD, packet.len() - off);
        let onair_len = 1 + chunk;
        let mut onair = Vec::with_capacity(onair_len);
        onair.push(if split { seq | 0x01 } else { seq });
        onair.extend_from_slice(&packet[off..off + chunk]);
        // 600 ms pre-PSEND drain: the RAK3172 rejects AT+PSEND with AT_BUSY_ERROR
        // if RX-disable (AT+PRECV=0) is still settling (native Reticulum waits
        // ~580 ms: uart_wait_tx_done(300) + drain_ms(80) + line), and a rejected
        // PSEND silently never radiates (the native logs per-frame OK/ERR; we
        // just never heard from the RNode when this was 80 ms).
        out.push(("AT+PRECV=0".to_string(), 600));
        out.push((alloc::format!("AT+PSEND={}", to_hex(&onair)), 1200));
        out.push(("AT+PRECV=65535".to_string(), 80));
        off += chunk;
        if off >= packet.len() {
            break;
        }
    }
    out
}

/// TX-side string built from `tx_lines` (for logging/tests).
pub fn tx_lines_hex(packet: &[u8], seq: u8) -> Vec<String> {
    tx_lines(packet, seq).into_iter().map(|(l, _)| l).collect()
}

/// RX drain: buffers UART lines, recognizes `+EVT:RXP2P` events, hex-decodes
/// the on-air frame, and reassembles whole RNS packets via the shared
/// RNode split-assembler.
pub struct AtRx {
    line_buf: String,
    /// after an +EVT header line, the next non-empty line is raw hex (the
    /// two-line RUI4 fallback; inline hex is preferred).
    expect_hex: bool,
    reasm: SplitAssembler,
}

impl AtRx {
    pub fn new() -> Self {
        Self {
            line_buf: String::new(),
            expect_hex: false,
            reasm: SplitAssembler::new(508, 15_000),
        }
    }

    /// Feed raw UART bytes; yields whole RNS packets (per event/line).
    pub fn feed(&mut self, bytes: &[u8], now_ms: u32) -> Vec<Vec<u8>> {
        let mut out = Vec::new();
        for &b in bytes {
            if b == b'\n' {
                let line = core::mem::take(&mut self.line_buf);
                let line = line.trim_end_matches('\r');
                if let Some(pkts) = self.handle_line(line, now_ms) {
                    out.extend(pkts);
                }
            } else {
                self.line_buf.push(b as char);
            }
        }
        out
    }

    fn handle_line(&mut self, line: &str, now_ms: u32) -> Option<Vec<Vec<u8>>> {
        if line.contains("+EVT:RXP2P") {
            self.expect_hex = true;
            // Inline payload after the last ':' on this same line.
            if let Some(hx) = event_hex(line) {
                let p = from_hex(hx)?;
                if !p.is_empty() {
                    self.expect_hex = false;
                    let got = self.push_onair(&p, now_ms);
                    return if got.is_empty() { None } else { Some(got) };
                }
            }
            return None; // two-line format: hex on the next line
        }
        if self.expect_hex && !line.is_empty() {
            self.expect_hex = false;
            let p = from_hex(line)?;
            if !p.is_empty() {
                let got = self.push_onair(&p, now_ms);
                return if got.is_empty() { None } else { Some(got) };
            }
        }
        None
    }

    /// Strip nothing — feed the on-air frame (RNode header + payload) into the
    /// shared demux + split reassembler.
    fn push_onair(&mut self, onair: &[u8], now_ms: u32) -> Vec<Vec<u8>> {
        let frame = parse_rnode_frame(onair);
        let res = self.reasm.push(&frame, now_ms);
        if res.complete {
            vec![res.data]
        } else {
            Vec::new()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use alloc::format;
    #[test]
    fn boot_sequence() {
        assert_eq!(
            boot_lines(),
            vec![
                "AT+PRECV=0",
                "AT+PFREQ=868000000",
                "AT+PSF=7",
                "AT+PBW=0",
                "AT+PCR=0",
                "AT+PPL=24",
                "AT+SYNCWORD=1424",
                "AT+PTP=17",
                "AT+PRECV=65535",
            ]
        );
    }

    #[test]
    fn hex_roundtrip() {
        assert_eq!(to_hex(b"\x01\xab\x0f"), "01ab0f");
        assert_eq!(from_hex("01ab0f"), Some(vec![1, 0xab, 0x0f]));
        assert_eq!(from_hex("abc"), None);
        assert_eq!(from_hex("zz"), None);
    }

    #[test]
    fn event_hex_extracts_payload() {
        assert_eq!(event_hex("+EVT:RXP2P:-18:13:010000da"), Some("010000da"));
        assert_eq!(event_hex("+EVT:RXP2P:-18:13:"), Some(""));
    }

    #[test]
    fn tx_single_frame() {
        let pkt = b"hello-sprout";
        let lines = tx_lines(pkt, 0xc0);
        // PRECV off → PSEND (header 0xc0 ‖ payload) → PRECV on.
        assert_eq!(lines[0].0, "AT+PRECV=0");
        assert_eq!(lines[1].0, format!("AT+PSEND=c0{}", to_hex(pkt)));
        assert_eq!(lines[2].0, "AT+PRECV=65535");
        assert_eq!(lines.len(), 3);
    }

    #[test]
    fn tx_split_three_frames_all_flagged() {
        // 300 B payload (> 128, the RAK's measured per-PSEND cap) → three
        // frames, all headers flagged (seq|0x01) → RNode SplitAssembler joins.
        let pkt: Vec<u8> = (0u16..300u16).map(|i| i as u8).collect();
        let seq = 0x20;
        let frames: Vec<String> = tx_lines(&pkt, seq)
            .into_iter()
            .filter(|(l, _)| l.starts_with("AT+PSEND="))
            .map(|(l, _)| l)
            .collect();
        assert_eq!(frames.len(), 3);
        let f0 = from_hex(frames[0].strip_prefix("AT+PSEND=").unwrap()).unwrap();
        let f1 = from_hex(frames[1].strip_prefix("AT+PSEND=").unwrap()).unwrap();
        let f2 = from_hex(frames[2].strip_prefix("AT+PSEND=").unwrap()).unwrap();
        for f in [&f0, &f1, &f2] {
            assert_eq!(f[0] & 0x01, 1, "every split frame flagged");
        }
        assert_eq!(f0.len(), 1 + DTU_FRAME_PAYLOAD);
        assert_eq!(f1.len(), 1 + DTU_FRAME_PAYLOAD);
        assert_eq!(f2.len(), 1 + (300 - 2 * DTU_FRAME_PAYLOAD));
        assert_eq!(&f0[1..], &pkt[..DTU_FRAME_PAYLOAD]);
        assert_eq!(&f2[1..], &pkt[2 * DTU_FRAME_PAYLOAD..]);
    }

    #[test]
    fn rx_inline_event_reassembles_packet() {
        let mut rx = AtRx::new();
        // A server single-frame: header 0x50 ‖ "payload-bytes".
        let payload = b"server-link-frame";
        let mut onair = vec![0x50u8];
        onair.extend_from_slice(payload);
        let line = alloc::format!(";+EVT:RXP2P:-17:11:{}\r\n", to_hex(&onair));
        let pkts = rx.feed(line.as_bytes(), 1000);
        assert!(pkts.len() == 1);
        assert_eq!(pkts[0], payload);
    }

    #[test]
    fn rx_two_line_fallback() {
        let mut rx = AtRx::new();
        let payload = b"two-line-format";
        let mut onair = vec![0xa0u8];
        onair.extend_from_slice(payload);
        // Header line, then the raw hex on the next (newline-terminated) line.
        rx.feed(b";+EVT:RXP2P:-10:9:\r\n", 1000);
        let pkts = rx.feed(alloc::format!("{}\r\n", to_hex(&onair)).as_bytes(), 1010);
        assert!(pkts.len() == 1);
        assert_eq!(pkts[0], payload);
    }

    #[test]
    fn rx_split_pair_reassembles() {
        let mut rx = AtRx::new();
        let payload: Vec<u8> = (0u16..260u16).map(|i| i as u8).collect();
        let mut f0 = vec![0x31u8]; // split-flagged
        f0.extend_from_slice(&payload[..254]);
        let mut f1 = vec![0x31u8];
        f1.extend_from_slice(&payload[254..]);
        let l0 = alloc::format!(";+EVT:RXP2P:-15:10:{}\r\n", to_hex(&f0));
        let l1 = alloc::format!(";+EVT:RXP2P:-15:10:{}\r\n", to_hex(&f1));
        assert!(rx.feed(l0.as_bytes(), 5000).is_empty());
        let pkts = rx.feed(l1.as_bytes(), 6000);
        assert!(pkts.len() == 1);
        assert_eq!(pkts[0], payload);
    }
}
