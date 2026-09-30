//! Outbound LXMF **wire format** to the LMAO server: pack a message, then
//! encrypt the **opportunistic slice** `packed[16..]` (the contract this crate
//! exists to pin — see the crate docs and `LXMF-WIRE-GATE.md`).

use alloc::vec::Vec;

use rns_core::constants::{
    DESTINATION_SINGLE, HEADER_1, PACKET_TYPE_DATA,
};
use rns_core::destination as rns_dest;
use rns_core::packet::{PacketFlags, RawPacket};
use rns_crypto::identity::Identity;

/// Truncated LXMF delivery destination hash for this identity — the address
/// the server replies to (the announce destination for `lxmf.delivery`).
pub fn lxmf_delivery_hash(identity: &Identity) -> [u8; 16] {
    rns_dest::destination_hash("lxmf", &["delivery"], Some(identity.hash()))
}

/// The packed LXMF wire message + the encrypted RNS payload *as python LXMF
/// puts it on the air*. Exposing both lets a host test pin the exact invariant:
/// `server.decrypt(ciphertext) == packed[DESTINATION_LENGTH..]`.
pub struct PackedEnvelope {
    /// Full LXMF wire message: `dest_hash ‖ src_hash ‖ signature ‖ payload`.
    pub packed: Vec<u8>,
    /// `encrypt(packed[DESTINATION_LENGTH..])` — what the RNS DATA packet
    /// actually carries (no leading destination hash).
    pub ciphertext: Vec<u8>,
    /// The sending side's `lxmf.delivery` hash (the wire `src_hash`).
    pub src_hash: [u8; 16],
}

/// Pack + opportunistically-encrypt an LXMF message to the server.
///
/// `server_dest` is the destination's 16-byte backing-hash (the server's
/// `lxmf.delivery`); `server_pubkey` its 64-byte public identity key. The
/// message source is this identity's `lxmf.delivery` hash — the hash the
/// server attributes (and replies to) — not the raw identity hash.
///
/// Returns `None` if packing/signing/encryption fails (the caller logs and
/// moves on rather than halt).
pub fn pack_lxmf_to_server(
    identity: &Identity,
    rng: &mut dyn rns_crypto::Rng,
    server_dest: &[u8; 16],
    server_pubkey: &[u8; 64],
    title: &[u8],
    content: &[u8],
    timestamp: f64,
) -> Option<PackedEnvelope> {
    let src_hash = lxmf_delivery_hash(identity);

    let packed = lxmf_core::message::pack(
        server_dest,
        &src_hash,
        timestamp,
        title,
        content,
        Vec::new(),
        None, // no stamp
        |data| identity.sign(data).map_err(|_| lxmf_core::message::Error::SignError),
    )
    .ok()?
    .packed;

    // LXMF opportunistic wire: the encrypted payload carries the message
    // WITHOUT the leading destination hash; the receiver re-prepends it from
    // the packet header (LXMRouter.delivery_packet). Encrypting the full
    // `packed` (dest included) misaligns the server's unpack → it drops the
    // message. This slice is the L1 fix.
    debug_assert!(
        packed.len() >= lxmf_core::constants::DESTINATION_LENGTH,
        "packed must lead with the destination hash"
    );
    let server = Identity::from_public_key(server_pubkey);
    let ciphertext = server
        .encrypt(&packed[lxmf_core::constants::DESTINATION_LENGTH..], rng)
        .ok()?;

    Some(PackedEnvelope { packed, ciphertext, src_hash })
}

/// Build a signed, encrypted LXMF message to the server, wrapped as an RNS
/// DATA packet addressed to the server's `lxmf.delivery` hash — ready to
/// transmit on-air.
pub fn build_lxmf_to_server(
    identity: &Identity,
    rng: &mut dyn rns_crypto::Rng,
    server_dest: &[u8; 16],
    server_pubkey: &[u8; 64],
    title: &[u8],
    content: &[u8],
    timestamp: f64,
) -> Option<Vec<u8>> {
    let env = pack_lxmf_to_server(
        identity, rng, server_dest, server_pubkey, title, content, timestamp,
    )?;
    let flags = PacketFlags {
        header_type: HEADER_1,
        context_flag: 0,
        transport_type: 0,
        destination_type: DESTINATION_SINGLE,
        packet_type: PACKET_TYPE_DATA,
    };
    RawPacket::pack(flags, 0, server_dest, None, 0, &env.ciphertext)
        .ok()
        .map(|p| p.raw)
}

/// The production LMAO "Hello" message: `LMAOEnvelope{text}` under the
/// `p:Envelope` title, exactly as the stable Cardputer leaf sends it.
pub fn build_message_to_server(
    identity: &Identity,
    rng: &mut dyn rns_crypto::Rng,
    server_dest: &[u8; 16],
    server_pubkey: &[u8; 64],
    content: &[u8],
    timestamp: f64,
) -> Option<Vec<u8>> {
    build_lxmf_to_server(
        identity, rng, server_dest, server_pubkey, b"p:Envelope", content, timestamp,
    )
}
