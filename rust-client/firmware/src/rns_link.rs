//! LMAO firmware RNS link: wires the SX1262 driver under the `RadioInterface`
//! contract and the RNode LoRa RF framing (the MicroPython `lora.py` transport
//! path, now in Rust).
//!
//! - **TX**: any RNS packet bytes → `split_into_frames` (1-byte RNode header,
//!   254-B frames, split into ≤2 frames) → transmit each via `prepare_send` +
//!   `start_tx` with a bounded RTC timeout, waiting for real `TX_DONE`, then
//!   re-arm continuous RX.
//! - **RX**: poll `RX_DONE`, read the frame, `parse_rnode_frame` → feed the
//!   `SplitAssembler` (reassembles the split pair the way the C++ RNode does),
//!   and on a complete RNS packet hand it to
//!   `RadioInterface::process_incoming` — which decodes the raw RNS packet and
//!   stamps RSSI/SNR on it (the leaf's signal source).
//!
//! All waits are real milliseconds (`esp_hal::delay::Delay`) — spin loops elide
//! at `opt-level = "s"` and that previously shrank the TX poll below the frame
//! airtime.

extern crate alloc;

use alloc::vec::Vec;

use radio_interface::interface::RadioInterface;
use radio_interface::{parse_rnode_frame, split_into_frames, SplitAssembler};
use rns_core::announce::AnnounceData;
use rns_core::destination as rns_dest;
use rns_core::packet::{PacketFlags, RawPacket};
use sx126x::irq;
use sx126x::{RadioBus, Sx1262};

/// `rns_crypto::Rng` adapter over the ESP32-S3 hardware RNG (used for the
/// ephemeral X25519 key in encryption-to-server).
pub struct EspRng(pub esp_hal::rng::Rng);

impl rns_crypto::Rng for EspRng {
    fn fill_bytes(&mut self, dest: &mut [u8]) {
        for c in dest.chunks_mut(4) {
            let r = self.0.random();
            c.copy_from_slice(&r.to_le_bytes()[..c.len()]);
        }
    }
}

/// The leaf node's persistent Ed25519 identity seed (64 B: 32-byte seed + 32-byte
/// public key, the RNS private-key layout). A production leaf would load this
/// from secure storage; here it is a fixed constant so the node keeps the same
/// RNS identity/destination across boots (like the persisted MicroPython
/// identity).
const NODE_IDENTITY_SEED: [u8; 64] = [
    0x1f, 0x8a, 0x4c, 0xd2, 0x77, 0x9e, 0x3b, 0x51, 0x06, 0x2f, 0x94, 0xbe, 0x60, 0xc5, 0x11, 0x9d,
    0x5e, 0x24, 0x7a, 0x09, 0xb3, 0x46, 0x8f, 0xd1, 0xe2, 0x38, 0x03, 0xfc, 0x0a, 0x8b, 0x66, 0x14,
    0xe9, 0x31, 0x90, 0x2c, 0x6f, 0x5b, 0xd8, 0x47, 0x52, 0x7e, 0x0d, 0xa4, 0xc9, 0x1a, 0x57, 0x90,
    0x66, 0x7b, 0x85, 0x34, 0xfb, 0x16, 0x0e, 0x92, 0xd3, 0x6a, 0xe0, 0x49, 0x3c, 0xc1, 0x79, 0x2b,
];

/// The leaf's RNS app/aspect name (its announce destination).
const APP_NAME: &str = "lmao";
const LEAF_ASPECT: &str = "leaf";

/// The LMAO server's LXMF delivery destination hash — derived from the
/// server's persisted identity (`~/.local/share/lmao_server/lxmf/identity`)
/// exactly as `lma_core.server_identity.delivery_destination_hash_hex` does,
/// verified byte-exact against the sprout deployment's
/// `server delivery hash=dad35b80164b25f7b1474be86e443702`.
pub const SERVER_LXMF_DELIVERY_HASH: [u8; 16] = [
    0xda, 0xd3, 0x5b, 0x80, 0x16, 0x4b, 0x25, 0xf7, 0xb1, 0x47, 0x4b, 0xe8, 0x6e, 0x44, 0x37, 0x02,
];

/// The server's RNS public identity key (64 B, from the same identity file) —
/// the key we encrypt outbound single-destination messages to (X25519).
pub const SERVER_PUBLIC_KEY: [u8; 64] = [
    0x19, 0x85, 0xac, 0x0e, 0xf9, 0x8f, 0x17, 0xd2, 0x66, 0x71, 0xf2, 0xf9, 0xea, 0x31, 0xc0, 0x59,
    0x3a, 0x90, 0xfe, 0xc1, 0xa3, 0x05, 0x79, 0xfa, 0x68, 0x54, 0x5a, 0x0c, 0xc0, 0x15, 0x94, 0x20,
    0x78, 0x90, 0x18, 0x21, 0x3f, 0x31, 0x15, 0x23, 0x9d, 0x0e, 0xd1, 0x03, 0x6c, 0xf3, 0x37, 0x5e,
    0xe7, 0x5c, 0xd4, 0x10, 0x31, 0x8e, 0xda, 0x87, 0xfb, 0x94, 0x20, 0xce, 0x7a, 0x4c, 0xa7, 0x88,
];

/// Longest whole RNS packet the radio carries (matching RNode: 2×254).
const MAX_PACKET: usize = 508;
/// Split-fragment reassembly timeout (ms) — mirrors `_REASM_TIMEOUT` × 1000.
const REASM_TIMEOUT_MS: u32 = 15_000;

/// Provision the node identity from its fixed seed and return its RNS
/// destination hash (`truncated(sha256(name_hash ‖ identity_hash))` — the
/// announce destination for `app.aspect`).
pub fn node_destination_hash() -> ([u8; 16], rns_crypto::identity::Identity) {
    let identity = rns_crypto::identity::Identity::from_private_key(&NODE_IDENTITY_SEED);
    let aspects = [LEAF_ASPECT];
    let dh = rns_dest::destination_hash(APP_NAME, &aspects, Some(identity.hash()));
    (dh, identity)
}

/// Build a signed RNS **announce** packet for the node's `app.aspect`
/// destination (byte-exact with the µReticulum `Destination.announce` wire
/// format): flags=ANNOUNCE/HDR1-config, hops=0, header destination_hash,
/// context=0, payload = pubkey ‖ name_hash ‖ random_hash ‖ signature. The
/// signature covers dest_hash ‖ pubkey ‖ name_hash ‖ random_hash.
///
/// `rng` supplies fresh bytes for random_hash so every announce differs
/// (replay/reassembly-freshness). Returns the raw wire packet, or `None` if
/// the packet can't be built — the caller logs and moves on rather than
/// panic-halting the node.
pub fn build_announce(
    identity: &rns_crypto::identity::Identity,
    random_hash: [u8; 10],
) -> Option<Vec<u8>> {
    use rns_core::constants::{DESTINATION_SINGLE, HEADER_1, PACKET_TYPE_ANNOUNCE};

    announce_for(identity, APP_NAME, LEAF_ASPECT, random_hash)
}

/// Build a signed RNS announce for `name.aspect` of this identity (byte-exact
/// with the µReticulum `Destination.announce` wire format — see
/// `build_announce`). Used to announce both the `lmao.leaf` presence
/// destination and the `lxmf.delivery` destination (so the server can address
/// replies back to this leaf).
pub fn announce_for(
    identity: &rns_crypto::identity::Identity,
    app_name: &str,
    aspect: &str,
    random_hash: [u8; 10],
) -> Option<Vec<u8>> {
    use rns_core::constants::{DESTINATION_SINGLE, HEADER_1, PACKET_TYPE_ANNOUNCE};

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

/// Build + encrypt a real LXMF message to the LMAO server's `lxmf.delivery`
/// destination ready to transmit on-air:
///   1. `lxmf_core::message::pack` — LNMF envelope addressed to the server,
///      signed by our identity, carrying `content` (any bytes).
///   2. X25519-encrypt the packed message to the server's public key
///      (ephemeral ‖ token-ciphertext), exactly as RNS single-destination
///      encryption.
///   3. Address an RNS DATA packet to the server's LXMF delivery hash.
/// Returns the raw wire packet for `RnsLink::send`.
pub fn build_message_to_server(
    identity: &rns_crypto::identity::Identity,
    rng: &mut dyn rns_crypto::Rng,
    content: &[u8],
    timestamp: f64,
) -> Option<Vec<u8>> {
    use rns_core::constants::{DESTINATION_SINGLE, HEADER_1, PACKET_TYPE_DATA};

    let src_hash = *identity.hash();

    let mut fields = Vec::new();
    let packed = lxmf_core::message::pack(
        &SERVER_LXMF_DELIVERY_HASH,
        &src_hash,
        timestamp,
        b"mesh",
        content,
        fields,
        None, // no stamp
        |data| identity.sign(data).map_err(|_| lxmf_core::message::Error::SignError),
    )
    .ok()?
    .packed;

    let server = rns_crypto::identity::Identity::from_public_key(&SERVER_PUBLIC_KEY);
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

/// The firmware's RNS link: one radio + one interface + the frame demux.
pub struct RnsLink<B: RadioBus> {
    radio: Sx1262<B>,
    interface: RadioInterface,
    assembler: SplitAssembler,
}

impl<B: RadioBus> RnsLink<B> {
    pub fn new(radio: Sx1262<B>, name: &str) -> Self {
        let mut interface = RadioInterface::new(name);
        interface.online = true;
        RnsLink {
            radio,
            interface,
            assembler: SplitAssembler::new(MAX_PACKET, REASM_TIMEOUT_MS),
        }
    }

    /// Borrow the interface (for stats / signal).
    pub fn interface(&self) -> &RadioInterface {
        &self.interface
    }

    /// Mutable access to the underlying radio (for hard-reset recovery).
    pub fn radio_mut(&mut self) -> &mut Sx1262<B> {
        &mut self.radio
    }

    /// Put the radio into continuous RX (the leaf's idle/listen state).
    pub fn arm_rx(&mut self) -> Result<(), ()> {
        self.radio.start_rx([0xFF, 0xFF, 0xFF]).map_err(|_| ())
    }

    /// Drop the radio to STANDBY_RC (XOSC/PLL off — the power-saving state for
    /// the dormant gap between link windows). `prepare_send` re-warms it via
    /// STDBY_XOSC before the next TX.
    pub fn enter_standby(&mut self) -> Result<(), ()> {
        self.radio.standby().map_err(|_| ())
    }

    /// Transmit an RNS packet (any bytes) over the LoRa link. Splits into RNode
    /// frames, transmits each with a real TX_DONE wait and a bounded RTC
    /// timeout (so a wedged sequencer is force-aborted instead of hanging),
    /// then re-arms continuous RX. `now_ms` is a monotonic millisecond clock.
    pub fn send(&mut self, data: &[u8], now_ms: u32) -> Result<(), ()> {
        if data.len() > MAX_PACKET {
            return Err(());
        }
        self.interface.note_outgoing(data);

        let frames = split_into_frames(data);
        for frame in &frames {
            self.radio.prepare_send(frame).map_err(|_| ())?;
            // Bounded TX: airtime is ~55 ms for a 31 B frame but up to ~175 ms
            // for a 148 B announce (preamble 24). Use a 400 ms timeout
            // (0x6400 * 15.625 us) so larger frames don't false-timeout, while
            // still force-aborting a wedged sequencer.
            self.radio.start_tx_timeout([0x00, 0x64, 0x00]).map_err(|_| ())?;
            if !self.wait_tx_done() {
                // Recover the radio (hard reset + reconfigure) happens up-stack.
                return Err(());
            }
            self.radio.clear_irq().ok();
        }
        self.radio.start_rx([0xFF, 0xFF, 0xFF]).ok();
        let _ = now_ms;
        Ok(())
    }

    /// Poll the radio for a received frame and, when a whole RNS packet is
    /// assembled, decode it through the interface. Returns number of LoRa
    /// frames consumed. `now_ms` is the current monotonic millisecond clock.
    pub fn pump_rx(&mut self, now_ms: u32) -> u32 {
        let mut consumed = 0;
        loop {
            let flags = match self.radio.get_irq_status() {
                Ok(v) => v,
                Err(_) => return consumed,
            };
            if flags & irq::RX_DONE == 0 {
                return consumed;
            }
            let ok = irq::rx_success(flags);
            let (len, ptr) = self.radio.get_rx_buffer_status().unwrap_or((0, 0));

            let mut frame = [0u8; 255];
            let n = self.radio.read_buffer(ptr, &mut frame[..len as usize]).unwrap_or(0);
            self.radio.clear_irq().ok();

            let parsed = parse_rnode_frame(&frame[..n]);
            if !ok {
                // CRC/header error — the preamble sync may still have run; drop.
                consumed += 1;
                self.radio.start_rx([0xFF, 0xFF, 0xFF]).ok();
                continue;
            }

            let mut rssi = 0i16;
            let mut snr = 0.0f32;
            if let Ok((r, s)) = self.radio.get_packet_status() {
                rssi = r;
                snr = s;
            }
            self.interface.rssi = Some(rssi);
            self.interface.snr = Some(snr);

            let res = self.assembler.push(&parsed, now_ms);
            consumed += 1;
            esp_println::println!(
                "[rns] rx frame n={} split={} rssi={}dBm snr={}dB complete={} stale={}",
                parsed.payload.len(),
                parsed.split,
                rssi,
                snr,
                res.complete,
                res.stale
            );
            if res.complete {
                match self.interface.process_incoming(&res.data, now_ms as u64) {
                    Ok(pkt) => esp_println::println!(
                        "[rns] RNS packet len={} type={:#x} hops={} dst={:02x?} hash={:02x?} dist_rssi={:?}",
                        pkt.raw.len(),
                        pkt.flags.pack(),
                        pkt.hops,
                        &pkt.destination_hash[..8],
                        &pkt.packet_hash[..8],
                        pkt.rssi
                    ),
                    Err(e) => esp_println::println!(
                        "[rns] raw frame {:02x?} (not a decodable RNS packet: {e:?})",
                        &res.data[..res.data.len().min(24)]
                    ),
                }
            }
            self.radio.start_rx([0xFF, 0xFF, 0xFF]).ok();
        }
    }

    /// Poll IRQ (real 5 ms) until TX_DONE or the 200 ms RTC timeout fires.
    fn wait_tx_done(&mut self) -> bool {
        use esp_hal::delay::Delay;
        for _ in 0..100 {
            match self.radio.get_irq_status() {
                Ok(v) => {
                    if v & irq::TX_DONE != 0 {
                        return true;
                    }
                    if v & irq::TIMEOUT != 0 {
                        return false;
                    }
                }
                Err(_) => return false,
            }
            Delay::new().delay_millis(5);
        }
        false
    }
}
