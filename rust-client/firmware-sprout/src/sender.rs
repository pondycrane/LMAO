//! Sprout RNS sender — the Sprout node's identity, signed announces and LXMF
//! SensorReport messages to the Rust LMAO server, on the DTU (RAK3172) radio.
//!
//! Port of the send-half of the Cardputer firmware's `rns_link.rs`, adapted to
//! the Sprout's own identity + `lmao/sprout` + `lxmf.delivery` aspects.  The
//! wire packets are identical to the µReticulum/rust RNS core the server runs,
//! so the DTU-transmitted frames are interchangeable with the native-client's.

use alloc::string::String;
use alloc::vec::Vec;

use rns_core::announce::AnnounceData;
use rns_core::constants::{DESTINATION_SINGLE, HEADER_1, PACKET_TYPE_ANNOUNCE, PACKET_TYPE_DATA};
use rns_core::destination as rns_dest;
use rns_core::packet::{PacketFlags, RawPacket};
use rns_crypto::identity::Identity;

/// The Sprout node's RNS app name + aspects (its announced destinations).
pub const APP_NAME: &str = "lmao";
pub const NODE_ASPECT: &str = "sprout";
pub const DELIVERY_ASPECT: &str = "delivery";
pub const DELIVERY_APP: &str = "lxmf";

/// The Rust Sprout node's persistent Ed25519 identity seed (64 B: 32-byte seed
/// + 32-byte public key, the RNS private-key layout).  A fixed constant so the
/// node keeps the same RNS identity across boots — like the native-client's
/// NVS-persisted identity.  **The resulting `lxmf.delivery` hash must be added
/// to the server's `LMAO_ALLOWED_CLIENTS`** (see the docs; the hash is printed
/// at boot + derived in the host workspace).
/// The persistent RNS identity of the PRODUCTION Sprout — the exact 64 B saved
/// by the native firmware's `lma_identity::load_or_create("sprout")` NVS key
/// `identity64` (read off this device's NVS partition). It yields the
/// allow-listed `lxmf.delivery` hash `f5f05952392627393f067df8c9eaf6c6`
/// (k8s/lmao-server-rust-app.yaml LMAO_ALLOWED_CLIENTS). A freshly-invented
/// seed is silently DROPPED by the server's allow-list gate — the old 0x516b…
/// constant produced `6f876d40…`, which was never whitelisted, so the Sprout
/// read as `<unknown>` on the server.
const SPROUT_IDENTITY_SEED: [u8; 64] = [
    0xff, 0x70, 0x4f, 0x43, 0x9b, 0x7f, 0xc8, 0xc4, 0x97, 0xf3, 0x19, 0xcb, 0x5d, 0x8b, 0xfb, 0xbd,
    0xb7, 0x9d, 0x23, 0x9c, 0xe6, 0xb8, 0x3f, 0x82, 0x32, 0x9e, 0x77, 0xe2, 0x40, 0xdb, 0x6f, 0xfa,
    0x06, 0xba, 0x21, 0x99, 0xfb, 0x02, 0x2e, 0x42, 0x46, 0x1f, 0xda, 0xfe, 0xd1, 0x1a, 0x60, 0x4b,
    0xf4, 0x7d, 0xb0, 0xd2, 0x65, 0x26, 0xf6, 0x3c, 0x38, 0x88, 0xe2, 0xdc, 0xab, 0xff, 0x2a, 0xbd,
];

/// The LMAO server's LXMF delivery destination hash — derived from the
/// server's persisted identity, verified byte-exact against the deployment's
/// `server delivery hash=dad35b80164b25f7b1474be86e443702` (same constant as
/// the Cardputer firmware's `rns_link.rs`).
pub const SERVER_LXMF_DELIVERY_HASH: [u8; 16] = [
    0xda, 0xd3, 0x5b, 0x80, 0x16, 0x4b, 0x25, 0xf7, 0xb1, 0x47, 0x4b, 0xe8, 0x6e, 0x44, 0x37, 0x02,
];

/// The server's RNS public identity key (the key we X25519-encrypt outbound
/// single-destination messages to — same constant as the Cardputer firmware).
pub const SERVER_PUBLIC_KEY: [u8; 64] = [
    0x19, 0x85, 0xac, 0x0e, 0xf9, 0x8f, 0x17, 0xd2, 0x66, 0x71, 0xf2, 0xf9, 0xea, 0x31, 0xc0, 0x59,
    0x3a, 0x90, 0xfe, 0xc1, 0xa3, 0x05, 0x79, 0xfa, 0x68, 0x54, 0x5a, 0x0c, 0xc0, 0x15, 0x94, 0x20,
    0x78, 0x90, 0x18, 0x21, 0x3f, 0x31, 0x15, 0x23, 0x9d, 0x0e, 0xd1, 0x03, 0x6c, 0xf3, 0x37, 0x5e,
    0xe7, 0x5c, 0xd4, 0x10, 0x31, 0x8e, 0xda, 0x87, 0xfb, 0x94, 0x20, 0xce, 0x7a, 0x4c, 0xa7, 0x88,
];

/// `rns_crypto::Rng` adapter over the ESP32 hardware RNG (ephemeral X25519).
pub struct EspRng(pub esp_hal::rng::Rng);

impl rns_crypto::Rng for EspRng {
    fn fill_bytes(&mut self, dest: &mut [u8]) {
        for c in dest.chunks_mut(4) {
            let r = self.0.random();
            c.copy_from_slice(&r.to_le_bytes()[..c.len()]);
        }
    }
}

/// Derived Sprout identity + the hashes the mesh + server key on.
pub struct SproutKeys {
    pub identity: Identity,
    /// announce destination hash for `lmao.sprout`.
    pub node_dest_hash: [u8; 16],
    /// announce destination hash for `lxmf.delivery` (the server whitelists /
    /// attributes this) — printed at boot for the allow-list.
    pub delivery_hash: [u8; 16],
    /// Hex of the identity hash — the `node_id` field of every SensorReport.
    pub node_id: String,
}

fn hex16(b: &[u8]) -> String {
    let mut s = String::with_capacity(b.len() * 2);
    for &x in b {
        s.push(char::from_digit((x >> 4) as u32, 16).unwrap_or('0'));
        s.push(char::from_digit((x & 0x0f) as u32, 16).unwrap_or('0'));
    }
    s
}

/// Provision the Sprout's persistent identity + all derived hashes.
pub fn provision() -> SproutKeys {
    let identity = Identity::from_private_key(&SPROUT_IDENTITY_SEED);
    let dh = rns_dest::destination_hash(APP_NAME, &[NODE_ASPECT], Some(identity.hash()));
    let delivery = destination_hash(DELIVERY_APP, &[DELIVERY_ASPECT], identity.hash());
    let node_id = hex16(identity.hash());
    SproutKeys {
        identity,
        node_dest_hash: dh,
        delivery_hash: delivery,
        node_id,
    }
}

/// RNS destination hash for `app.aspect` of this identity (announce target).
pub fn destination_hash(app: &str, aspects: &[&str], identity_hash: &[u8; 16]) -> [u8; 16] {
    rns_dest::destination_hash(app, aspects, Some(identity_hash))
}

/// Build a signed RNS announce for `app.aspect` of the Sprout identity
/// (byte-exact with the µReticulum `Destination.announce` wire format).
pub fn announce_for(
    identity: &Identity,
    app_name: &str,
    aspect: &str,
    random_hash: [u8; 10],
) -> Option<Vec<u8>> {
    let aspects = [aspect];
    let nh = rns_dest::name_hash(app_name, &aspects);
    let dh = rns_dest::destination_hash(app_name, &aspects, Some(identity.hash()));
    let (announce_data, _) = AnnounceData::pack(identity, &dh, &nh, &random_hash, None, None).ok()?;
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

/// µReticulum `random_hash` layout: `urandom(5) ‖ unix_time(5, BE)`.
pub fn random_hash(random5: [u8; 5], unix_time: u64) -> [u8; 10] {
    let mut rh = [0u8; 10];
    rh[..5].copy_from_slice(&random5);
    rh[5..].copy_from_slice(&unix_time.to_be_bytes()[3..]);
    rh
}

/// Build + encrypt a real LXMF message to the server's `lxmf.delivery`
/// destination ready to transmit on-air (the Sprout's SensorReport envelope):
///   1. `lxmf_core::message::pack` — LNMF envelope addressed to the server,
///      signed by the Sprout identity, carrying `content`.
///   2. X25519-encrypt the packed message to the server's public key.
///   3. Address an RNS DATA packet to the server's LXMF delivery hash.
pub fn message_to_server(
    identity: &Identity,
    rng: &mut dyn rns_crypto::Rng,
    content: &[u8],
    timestamp: f64,
) -> Option<Vec<u8>> {
    // LXMF messages are between `lxmf.delivery` destinations: the source field
    // is OUR delivery destination hash (announced), which the server keys off.
    let src_hash = destination_hash(DELIVERY_APP, &[DELIVERY_ASPECT], identity.hash());
    let fields = Vec::new();
    let packed = lxmf_core::message::pack(
        &SERVER_LXMF_DELIVERY_HASH,
        &src_hash,
        timestamp,
        b"p:Envelope", // title the stable leaf uses for LMAO envelopes
        content,
        fields,
        None, // no stamp
        |data| identity.sign(data).map_err(|_| lxmf_core::message::Error::SignError),
    )
    .ok()?
    .packed;

    let server = Identity::from_public_key(&SERVER_PUBLIC_KEY);
    let ciphertext = server.encrypt(&packed, rng).ok()?;

    let flags = PacketFlags {
        header_type: HEADER_1,
        context_flag: 0,
        transport_type: 0,
        destination_type: DESTINATION_SINGLE,
        packet_type: PACKET_TYPE_DATA,
    };
    RawPacket::pack(flags, 0, &SERVER_LXMF_DELIVERY_HASH, None, 0, &ciphertext)
        .ok()
        .map(|p| p.raw)
}
