//! RNS **announce** construction + the reference `random_hash` layout.
//!
//! Byte-exact with µReticulum's `Destination.announce` wire format:
//! flags=ANNOUNCE/HDR1-config, hops=0, header destination_hash, context=0,
//! payload = pubkey ‖ name_hash ‖ random_hash ‖ signature; signature covers
//! dest_hash ‖ pubkey ‖ name_hash ‖ random_hash.

use alloc::vec::Vec;

use rns_core::announce::AnnounceData;
use rns_core::constants::{
    DESTINATION_SINGLE, HEADER_1, PACKET_TYPE_ANNOUNCE,
};
use rns_core::destination as rns_dest;
use rns_core::packet::{PacketFlags, RawPacket};
use rns_crypto::identity::Identity;

/// Build the leaf's default `lmao.leaf` presence announce.
///
/// `rng`-freshened `random_hash` makes every announce differ (so replay /
/// reassembly-freshness is meaningful). Returns the raw wire packet, or `None`
/// if it can't be built — the caller logs and moves on rather than halting.
pub fn build_announce(
    identity: &Identity,
    random_hash: [u8; 10],
) -> Option<Vec<u8>> {
    announce_for(identity, "lmao", "leaf", random_hash)
}

/// Build a signed RNS announce for `app_name.aspect` of this identity — the
/// `lmao.leaf` presence destination or the `lxmf.delivery` destination (so the
/// server can address replies back to the leaf).
pub fn announce_for(
    identity: &Identity,
    app_name: &str,
    aspect: &str,
    random_hash: [u8; 10],
) -> Option<Vec<u8>> {
    let aspects = [aspect];
    let nh = rns_dest::name_hash(app_name, &aspects);
    let dh = rns_dest::destination_hash(app_name, &aspects, Some(identity.hash()));

    let (announce_data, _has_ratchet) =
        AnnounceData::pack(identity, &dh, &nh, &random_hash, None, None).ok()?;

    let flags = PacketFlags {
        header_type: HEADER_1,
        context_flag: 0,
        transport_type: 0,
        destination_type: DESTINATION_SINGLE,
        packet_type: PACKET_TYPE_ANNOUNCE,
    };
    RawPacket::pack(flags, 0, &dh, None, 0, &announce_data)
        .ok()
        .map(|p| p.raw)
}

/// µReticulum reference `random_hash` layout: `urandom(5) ‖ unix_time(5, BE)`.
///
/// The first 5 bytes are sender-fresh random; the last 5 are the 5 most
/// significant bytes of the big-endian 64-bit Unix timestamp — the path-table
/// timebase the server derives announce recency/distance from (sending 10
/// fully random bytes is accepted on the wire but yields garbage timing).
pub fn random_hash(random5: [u8; 5], unix_time: u64) -> [u8; 10] {
    let mut rh = [0u8; 10];
    rh[..5].copy_from_slice(&random5);
    rh[5..].copy_from_slice(&unix_time.to_be_bytes()[3..]);
    rh
}
