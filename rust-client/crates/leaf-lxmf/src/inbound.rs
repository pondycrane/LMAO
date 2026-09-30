//! Inbound: decrypt + unpack the server's reply to our `lxmf.delivery` and
//! parse the piggybacked `DATA …` chart line.
//!
//! The reply is the mirror of the outbound opportunistic shape: the server
//! sends `encrypt(packed_reply[16..])`, so before `lxmf_core::message::unpack`
//! we must re-prepend **our** `lxmf.delivery` hash to the decrypted payload
//! (Python `LXMRouter.delivery_packet`: `lxmf_data = packet.destination.hash +
//! data`). This is the inbound L2 mirror fix, pinned here so it can't regress.

use alloc::string::String;
use alloc::vec::Vec;

use rns_crypto::identity::Identity;

use crate::envelope::decode_text_content;

/// no_std rounding (`f32::round` is std/libm): round half up for the
/// non-negative server values (moisture %, temperature). NaN saturates to 0.
pub fn roundi(x: f32) -> i64 {
    (x + 0.5) as i64
}

/// A parsed `DATA …` chart record (mirrors `chart.parse_data_line`).
#[derive(Debug, Clone, PartialEq)]
pub struct ChartRecord {
    pub node: String,
    pub dry: i64,
    pub wet: i64,
    pub temp: Vec<f32>,
    pub humidity: Vec<i64>,
    pub samples: Vec<i64>,
    pub water_mask: u64,
}

/// Extract the first `DATA ` record from *text* (the server piggybacks the
/// Sprout moisture series on its ACK), or None. Tolerates the ACK line sharing
/// the message and skips malformed records rather than failing.
pub fn parse_data_line(text: &str) -> Option<ChartRecord> {
    for raw in text.lines() {
        let line = raw.trim();
        if !line.starts_with("DATA ") {
            continue;
        }
        let all: Vec<&str> = line.split_whitespace().collect();
        if all.len() < 6 || all[0] != "DATA" {
            continue;
        }
        // Inner closure so a malformed record just aborts to "keep scanning".
        let rec = (|| -> Option<ChartRecord> {
            let node = String::from(all[1]);
            let dry = all[2].parse::<f32>().ok().map(roundi)?;
            let wet = all[3].parse::<f32>().ok().map(roundi)?;
            let ct = all.get(4)?.parse::<usize>().ok()?;
            let mut idx = 5;
            let mut temp = Vec::new();
            for _ in 0..ct {
                temp.push(all.get(idx)?.parse::<f32>().ok()?);
                idx += 1;
            }
            let ch = all.get(idx)?.parse::<usize>().ok()?;
            idx += 1;
            let mut humidity = Vec::new();
            for _ in 0..ch {
                humidity.push(all.get(idx)?.parse::<f32>().ok().map(roundi)?);
                idx += 1;
            }
            let cm = all.get(idx)?.parse::<usize>().ok()?;
            idx += 1;
            let mut samples = Vec::new();
            for _ in 0..cm {
                samples.push(all.get(idx)?.parse::<f32>().ok().map(roundi)?);
                idx += 1;
            }
            // Optional trailing watering mask (old lines lack it).
            let water_mask = match all.len() - idx {
                0 => 0u64,
                1 => all[idx].parse::<u64>().ok()?,
                _ => return None,
            };
            // A percent-less line can never come from the server — reject it
            // (mirrors the Python guard: `if not humidity and not samples`).
            if humidity.is_empty() && samples.is_empty() {
                return None;
            }
            Some(ChartRecord { node, dry, wet, temp, humidity, samples, water_mask })
        })();
        if rec.is_some() {
            return rec;
        }
    }
    None
}

/// A decrypted + unpacked + parsed server reply to one of our messages.
#[derive(Debug, Clone)]
pub struct Reply {
    /// The server's LXMF source hash (its `lxmf.delivery`).
    pub src_hash: [u8; 16],
    pub title: Vec<u8>,
    pub content: Vec<u8>,
    /// Whether the message signature verified against the server's public key.
    pub sig_valid: bool,
    /// Decoded `TextMessage.content` ("ACK …\nDATA …"), UTF-8 when valid.
    pub text: String,
    /// The chart record from that text, if parseable.
    pub chart: Option<ChartRecord>,
}

/// Decrypt + unpack an inbound DATA payload addressed to our `lxmf.delivery` —
/// the server's reply — applying the inbound mirror rule (`our_delivery_hash ‖
/// decrypt(payload)` before `unpack`), verifying the signature against the
/// server's public key, and parsing the piggybacked chart line.
///
/// `identity` is the client's identity (its private key decrypts the reply);
/// `our_delivery_hash` is `lxmf_delivery_hash(identity)`. Returns `None` if
/// decryption or unpack fails (not-for-us / bad key / corruprupled frame) — the
/// caller logs and skips.
pub fn decrypt_server_reply(
    identity: &Identity,
    payload: &[u8],
    server_pubkey: &[u8; 64],
    our_delivery_hash: &[u8; 16],
) -> Option<Reply> {
    // 1. X25519-decrypt the packet payload (encrypted to our public key).
    let plaintext = identity.decrypt(payload).ok()?;

    // 2. Opportunistic mirror: re-prepend OUR delivery hash before unpack (the
    //    server sent the message without the leading 16-byte destination).
    let mut lxmf_wire = Vec::with_capacity(our_delivery_hash.len() + plaintext.len());
    lxmf_wire.extend_from_slice(our_delivery_hash);
    lxmf_wire.extend_from_slice(&plaintext);

    // 3. LXMF unpack, verifying the signature against the server's key.
    let server = Identity::from_public_key(server_pubkey);
    let verify = |_src: &[u8; 16], sig: &[u8; 64], data: &[u8]| server.verify(sig, data);
    let msg = lxmf_core::message::unpack(&lxmf_wire, Some(&verify)).ok()?;

    // 4. The reply content is `LMAOEnvelope{text: TextMessage}` ("ACK …\nDATA
    //    …"). Pull the text out and parse the chart line.
    let text = decode_text_content(&msg.content)
        .and_then(|t| core::str::from_utf8(t).ok())
        .map(String::from)
        .unwrap_or_default();
    let chart = parse_data_line(&text);

    Some(Reply {
        src_hash: msg.source_hash,
        title: msg.title,
        content: msg.content,
        sig_valid: msg.signature_valid.unwrap_or(false),
        text,
        chart,
    })
}
