//! T5 LXMF control path (design §5 `lxmf`, ticket T5 controls).
//!
//! Packs a prost `LmaoEnvelope` (from `lma-wire`) into a signed LXMF message
//! addressed to the `lxmf/delivery` DEST and addressed-by the leaf identity's
//! source hash; unpacks + verifies on receipt. Content Title follows the proto
//! convention ("p:Envelope" signals protobuf-encoded Content — see
//! `proto/lma_messages.proto`). Host-testable: pack/unpack/verify round-trip
//! in `tests/` against the identity's own signature (Ed25519).

use lma_wire::LmaoEnvelope;
use prost::Message;
use rns_crypto::identity::Identity;

/// LXMF Content title that marks a protobuf-encoded LMAO envelope (proto doc).
pub const ENVELOPE_TITLE: &[u8] = b"p:Envelope";

/// Pack a signed LXMF message carrying a protobuf `LmaoEnvelope`.
///
/// `dest_hash` is the destination's 16-byte backing-hash (the `lxmf/delivery`
/// DEST from `lma_identity::delivery_hash` for the server). `identity` is the
/// leaf's signing identity; its `hash()` becomes the message source hash and
/// its Ed25519 key signs the message. Returns `(packed_bytes, message_hash)`.
pub fn pack_envelope(
    identity: &Identity,
    dest_hash: &[u8; 16],
    timestamp: f64,
    envelope: &LmaoEnvelope,
) -> Result<(Vec<u8>, [u8; 32]), lxmf_core::message::Error> {
    let mut content = Vec::new();
    envelope
        .encode(&mut content)
        .map_err(|_| lxmf_core::message::Error::InvalidPayload("LmaoEnvelope encode"))?;

    let lxmf_core::message::PackResult { packed, message_hash } = lxmf_core::message::pack(
        dest_hash,
        identity.hash(),
        timestamp,
        ENVELOPE_TITLE,
        &content,
        Vec::new(),
        None,
        |data| identity.sign(data).map_err(|_| lxmf_core::message::Error::SignError),
    )?;
    Ok((packed, message_hash))
}

/// Unpack an LXMF message, verify the source signature, and decode the envelope.
///
/// `source` is the identity whose public key verifies the message (matched by
/// `src_hash`). Returns `(envelope, unpack_result)`; fails if the signature is
/// absent/invalid or the content is not a decodable `LmaoEnvelope`.
pub fn unpack_envelope_verified(
    packed: &[u8],
    source: &Identity,
) -> Result<(LmaoEnvelope, lxmf_core::message::UnpackResult), lxmf_core::message::Error> {
    let res = lxmf_core::message::unpack(
        packed,
        Some(&|_src_hash, signature, signed| source.verify(signature, signed)),
    )?;

    if !res.signature_valid.unwrap_or(false) {
        return Err(lxmf_core::message::Error::SignError);
    }
    if res.title != ENVELOPE_TITLE {
        return Err(lxmf_core::message::Error::InvalidPayload("Not a p:Envelope content"));
    }

    let envelope = LmaoEnvelope::decode(res.content.as_slice())
        .map_err(|_| lxmf_core::message::Error::InvalidPayload("LmaoEnvelope decode"))?;
    Ok((envelope, res))
}
