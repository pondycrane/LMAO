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
///
/// This is the **canonical Cardputer client identity** — the exact 64 bytes of
/// `~/.local/share/lmao_client/lxmf/identity` (the file the repo's install
/// tooling pins onto the device at `/flash/rns/identity`, lma_core/
/// client_identity.py). It yields the server-whitelisted `lxmf.delivery` hash
/// `99ce32311dc37193eff4951a912f8f1b` and identity hash
/// `8a17668171337ca80177c7464f6c9020` — both are in
/// `LMAO_ALLOWED_CLIENTS` (k8s/lmao-server.yaml). A freshly-invented seed
/// previously made this node a *different* identity the server drops at the
/// allow-list gate, so no reply ever came back.
const NODE_IDENTITY_SEED: [u8; 64] = [
    0x28, 0x0a, 0xea, 0x11, 0xb8, 0x8c, 0x63, 0xb1,
    0xc9, 0x1f, 0x8b, 0x9c, 0x73, 0x8c, 0x13, 0x8f,
    0xd4, 0xe8, 0x7e, 0x5d, 0xd5, 0xe5, 0x29, 0xc3,
    0xab, 0x98, 0xd6, 0x20, 0x23, 0xbc, 0x9c, 0x75,
    0x31, 0x95, 0xcf, 0xa3, 0xa4, 0x89, 0xa7, 0xf4,
    0xa1, 0x7b, 0xa4, 0x12, 0xdf, 0x5b, 0xa5, 0xa9,
    0x0c, 0xde, 0x60, 0xd8, 0xe4, 0xe6, 0x7f, 0xc7,
    0xc1, 0x0c, 0xe0, 0x9a, 0xce, 0xd0, 0x42, 0xf8,
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
    // The stable leaf sends protobuf LMAO envelopes under the p:Envelope title.
    build_lxmf_to_server(identity, rng, b"p:Envelope", content, timestamp)
}

/// Pack + encrypt an LXMF message to the server with an explicit title.
fn build_lxmf_to_server(
    identity: &rns_crypto::identity::Identity,
    rng: &mut dyn rns_crypto::Rng,
    title: &[u8],
    content: &[u8],
    timestamp: f64,
) -> Option<Vec<u8>> {
    use rns_core::constants::{DESTINATION_SINGLE, HEADER_1, PACKET_TYPE_DATA};

    // LXMF messages are between `lxmf.delivery` destinations: the *source*
    // field is the sending side's delivery destination hash (its `lxmf.delivery`
    // OUT dest), NOT the raw identity hash. urns sends
    // `Destination(identity, OUT, SINGLE, "lxmf", "delivery")` as `self._source`
    // and packs `self._source.hash`; the server keys replies off that hash.
    // Previously we put the raw identity hash here, so the server couldn't
    // attribute messages to the (whitelisted) delivery destination.
    let src_hash = lxmf_delivery_hash(identity);

    let fields = Vec::new();
    let packed = lxmf_core::message::pack(
        &SERVER_LXMF_DELIVERY_HASH,
        &src_hash,
        timestamp,
        title,
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

// ── Minimal protobuf encoder (LMAO envelopes; no prost on-device) ──────────
// Wire types: 0=varint, 2=length-delimited, 5=fixed32 (little-endian).

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
/// Build the LMAO `LMAOEnvelope{ text: TextMessage }` protobuf bytes exactly
/// as the stable leaf's `encode_envelope_text(encode_text_message(...))`:
/// field 20 (TextMessage) with node_id, the text content and a millisecond
/// timestamp. This is the "Hello from Cardputer" text message.
pub fn build_text_envelope(node_id_hex: &str, content: &str, timestamp_ms: u64) -> Vec<u8> {
    let mut text_msg = Vec::new();
    pb_field_ld(&mut text_msg, 1, node_id_hex.as_bytes()); // node_id
    pb_field_ld(&mut text_msg, 2, content.as_bytes()); // content
    pb_field_var(&mut text_msg, 3, timestamp_ms); // timestamp (ms)

    let mut envelope = Vec::new();
    pb_field_ld(&mut envelope, 20, &text_msg); // FIELD_TEXT = 20
    envelope
}

// ── Inbound: server reply / chart data (the leaf's receive half) ─────────────
//
// The server ACKs every client message with an encrypted LXMF reply whose
// content is an `LMAOEnvelope{text: TextMessage}`; `TextMessage.content` is
// "ACK from LMAO Server — received your message (N bytes)" followed by a
// `DATA …` line carrying the recent Sprout moisture series, so the client
// charts it with no extra airtime. This mirrors the stable client's
// `handle_reply` + `chart.parse_data_line` (main.py).

/// µReticulum reference `random_hash` layout: `urandom(5) ‖ unix_time(5, BE)`.
/// The first 5 bytes are sender-fresh random; the last 5 are the 5 most
/// significant bytes of the big-endian 64-bit Unix timestamp (the path-table
/// timebase the server derives announce recency from).
pub fn random_hash(random5: [u8; 5], unix_time: u64) -> [u8; 10] {
    let mut rh = [0u8; 10];
    rh[..5].copy_from_slice(&random5);
    rh[5..].copy_from_slice(&unix_time.to_be_bytes()[3..]);
    rh
}

/// Truncated LXMF delivery destination hash for this identity — the address
/// the server replies to (the announce destination for `lxmf.delivery`).
pub fn lxmf_delivery_hash(identity: &rns_crypto::identity::Identity) -> [u8; 16] {
    rns_dest::destination_hash("lxmf", &["delivery"], Some(identity.hash()))
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
fn decode_text_content(envelope: &[u8]) -> Option<&[u8]> {
    let text_msg = find_pb_field_ld(envelope, 20)?; // FIELD_TEXT = 20
    find_pb_field_ld(text_msg, 2) // TextMessage.content = 2
}

/// Decrypt + unpack an inbound DATA packet addressed to our `lxmf.delivery` —
/// the server's reply to one of our messages — and, when it carries a `DATA …`
/// chart line, return the parsed record for the panel to display.  Logs each
/// stage on serial so RF bring-up stays visible without the LCD.
pub fn handle_lxmf_reply(
    identity: &rns_crypto::identity::Identity,
    pkt: &RawPacket,
) -> Option<lma_chart::ChartRecord> {
    // 1. X25519-decrypt the packet payload (encrypted to our public key).
    let plaintext = match identity.decrypt(&pkt.data) {
        Ok(p) => p,
        Err(_) => {
            esp_println::println!("[rns] inbound decrypt failed (bad key / not for us? — skip)");
            return None;
        }
    };
    // 2. LXMF unpack, verifying the signature against the server's key.
    let server = rns_crypto::identity::Identity::from_public_key(&SERVER_PUBLIC_KEY);
    let verify = |_src: &[u8; 16], sig: &[u8; 64], data: &[u8]| server.verify(sig, data);
    let msg = match lxmf_core::message::unpack(&plaintext, Some(&verify)) {
        Ok(m) => m,
        Err(e) => {
            esp_println::println!("[rns] inbound LXMF unpack failed: {e:?}");
            return None;
        }
    };
    esp_println::println!(
        "[rns] INBOUND lxmf src={:02x?} title={:?} sig_valid={:?} content={} B",
        &msg.source_hash[..8],
        core::str::from_utf8(&msg.title).unwrap_or("<bin>"),
        msg.signature_valid,
        msg.content.len()
    );
    // 3. The reply content is `LMAOEnvelope{text: TextMessage}`
    //    ("ACK …\nDATA …"). Pull the text out and parse the chart line.
    let Some(text) = decode_text_content(&msg.content) else { return None };
    let Ok(text) = core::str::from_utf8(text) else { return None };
    for line in text.lines().take(1) {
        esp_println::println!("[rns] reply: {line}");
    }
    let c = lma_chart::parse_data_line(text)?;
    esp_println::println!(
        "[rns] CHART node={} dry={} wet={} samples={} temp_count={} water_mask={:#x}",
        c.node, c.dry, c.wet, c.samples.len(), c.temp.len(), c.water_mask
    );
    Some(c)
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
        let total = frames.len();
        for (i, frame) in frames.iter().enumerate() {
            // DEBUG (temporary): per-frame TX visibility for the multi-frame
            // (split) message path — frame2 of the 307-byte Hello is going
            // missing on-air while frame1 (and single-frame announces) work.
            esp_println::println!("[dbg] send frame {}/{} len={}", i + 1, total, frame.len());
            self.radio.prepare_send(frame).map_err(|_| ())?;
            // Bounded TX: airtime is ~55 ms for a 31 B frame but up to ~175 ms
            // for a 148 B announce (preamble 24). Use a 400 ms timeout
            // (0x6400 * 15.625 us) so larger frames don't false-timeout, while
            // still force-aborting a wedged sequencer.
            self.radio.start_tx_timeout([0x00, 0x64, 0x00]).map_err(|_| ())?;
            let ok = self.wait_tx_done();
            esp_println::println!("[dbg] frame {}/{} tx_done={}", i + 1, total, ok);
            if !ok {
                // Recover the radio (hard reset + reconfigure) happens up-stack.
                return Err(());
            }
            self.radio.clear_irq().ok();
            // Back-to-back split frames give the receiver's LoRa demodulator no
            // AGC/sync turnaround; a short gap between them lets the next frame's
            // preamble lock (frame2 of a 2-frame message was being missed
            // on-air, truncating the packet and failing server decryption).
            if i + 1 < total {
                esp_hal::delay::Delay::new().delay_millis(50);
            }
        }
        self.radio.start_rx([0xFF, 0xFF, 0xFF]).ok();
        let _ = now_ms;
        Ok(())
    }

    /// Poll the radio for a received frame and, when a whole RNS packet is
    /// assembled, decode it through the interface. Returns number of LoRa
    /// frames consumed. `now_ms` is the current monotonic millisecond clock.
    /// `identity` is the node identity: an inbound DATA packet addressed to
    /// our `lxmf.delivery` hash is decrypted as the server's reply and parsed
    /// for the piggybacked chart data. Every decoded inbound packet is also
    /// offered to `dispatch` (used to route link handshake / resource packets
    /// to the LinkResource driver).
    pub fn pump_rx(
        &mut self,
        now_ms: u32,
        identity: &rns_crypto::identity::Identity,
        dispatch: &mut dyn FnMut(&RawPacket),
        on_chart: &mut dyn FnMut(&lma_chart::ChartRecord),
    ) -> u32 {
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
                    Ok(pkt) => {
                        esp_println::println!(
                            "[rns] RNS packet len={} type={:#x} hops={} dst={:02x?} hash={:02x?} dist_rssi={:?}",
                            pkt.raw.len(),
                            pkt.flags.pack(),
                            pkt.hops,
                            &pkt.destination_hash[..8],
                            &pkt.packet_hash[..8],
                            pkt.rssi
                        );
                        // The server's reply to us: an LXMF DATA message
                        // addressed to our lxmf.delivery destination. Decrypt
                        // + parse the piggybacked chart line, and hand the
                        // record to the panel (main owns the LCD).
                        if pkt.flags.packet_type == rns_core::constants::PACKET_TYPE_DATA
                            && pkt.destination_hash == lxmf_delivery_hash(identity)
                        {
                            if let Some(c) = handle_lxmf_reply(identity, &pkt) {
                                on_chart(&c);
                            }
                        }
                        dispatch(&pkt);
                    }
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
