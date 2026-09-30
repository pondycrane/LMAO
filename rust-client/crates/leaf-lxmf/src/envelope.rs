//! Minimal protobuf encoder/decoder for the LMAO text envelope — the leaf's
//! "Hello from Cardputer" message and the server's `ACK …\nDATA …` reply
//! payload. No prost on-device (keeps this crate no_std + allocation-only).
//!
//! Wire types used by LMAO: 0=varint, 2=length-delimited, 5=fixed32 (LE).

use alloc::vec::Vec;

fn pb_varint(buf: &mut Vec<u8>, mut v: u64) {
    while v >= 0x80 {
        buf.push((v as u8 & 0x7f) | 0x80);
        v >>= 7;
    }
    buf.push(v as u8);
}

fn pb_tag(buf: &mut Vec<u8>, field: u32, wire: u8) {
    pb_varint(buf, ((field as u64) << 3) | wire as u64);
}

fn pb_field_var(buf: &mut Vec<u8>, field: u32, v: u64) {
    pb_tag(buf, field, 0);
    pb_varint(buf, v);
}

fn pb_field_ld(buf: &mut Vec<u8>, field: u32, data: &[u8]) {
    pb_tag(buf, field, 2);
    pb_varint(buf, data.len() as u64);
    buf.extend_from_slice(data);
}

/// Build the LMAO `LMAOEnvelope{ text: TextMessage }` protobuf bytes exactly as
/// the stable leaf's `encode_envelope_text(encode_text_message(...))`:
/// field 20 (TextMessage) with node_id, text content and a millisecond
/// timestamp.
pub fn build_text_envelope(node_id_hex: &str, content: &str, timestamp_ms: u64) -> Vec<u8> {
    let mut text_msg = Vec::new();
    pb_field_ld(&mut text_msg, 1, node_id_hex.as_bytes()); // node_id
    pb_field_ld(&mut text_msg, 2, content.as_bytes()); // content
    pb_field_var(&mut text_msg, 3, timestamp_ms); // timestamp (ms)

    let mut envelope = Vec::new();
    pb_field_ld(&mut envelope, 20, &text_msg); // FIELD_TEXT = 20
    envelope
}

/// Minimal protobuf varint reader (pairs with `pb_varint`).
fn read_pb_varint(buf: &[u8], pos: &mut usize) -> Option<u64> {
    let mut res = 0u64;
    let mut shift = 0u32;
    loop {
        let b = *buf.get(*pos)?;
        *pos += 1;
        res |= ((b & 0x7f) as u64) << shift;
        if b & 0x80 == 0 {
            return Some(res);
        }
        shift += 7;
        if shift >= 64 {
            return None;
        }
    }
}

/// Body of the first length-delimited protobuf field `want` in `buf` (skips
/// other wire types defensively). `LMAOEnvelope`/`TextMessage` are small; a
/// linear scan is fine here.
fn find_pb_field_ld(buf: &[u8], want: u32) -> Option<&[u8]> {
    let mut pos = 0;
    while pos < buf.len() {
        let tag = read_pb_varint(buf, &mut pos)?;
        let field = (tag >> 3) as u32;
        let wire = (tag & 7) as u8;
        match wire {
            0 => {
                read_pb_varint(buf, &mut pos)?;
            }
            1 => pos = pos.checked_add(8)?,
            2 => {
                let len = read_pb_varint(buf, &mut pos)? as usize;
                let end = pos.checked_add(len)?;
                if end > buf.len() {
                    return None;
                }
                if field == want {
                    return Some(&buf[pos..end]);
                }
                pos = end;
            }
            5 => pos = pos.checked_add(4)?,
            _ => return None, // groups not used by LMAO
        }
    }
    None
}

/// Extract `TextMessage.content` from an `LMAOEnvelope{text}` protobuf
/// (field 20 → TextMessage field 2) — the server reply payload.
pub fn decode_text_content(envelope: &[u8]) -> Option<&[u8]> {
    let text_msg = find_pb_field_ld(envelope, 20)?; // FIELD_TEXT = 20
    find_pb_field_ld(text_msg, 2) // TextMessage.content = 2
}
