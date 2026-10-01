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
//! The **pure wire protocol** — RNS announce build, LXMF opportunistic
//! pack/encrypt, the text-envelope protobuf, and server-reply decode — lives
//! in the no_std `leaf-lxmf` crate (host-testable; see `crates/leaf-lxmf`).
//! This module only wires that logic to the radio + the ESP32-S3 RNG and
//! prints the verified INBOUND/reply/CHART results to the serial console.
//!
//! All waits are real milliseconds (`esp_hal::delay::Delay`) — spin loops elide
//! at `opt-level = "s"` and that previously shrank the TX poll below the frame
//! airtime.

extern crate alloc;

use radio_interface::interface::RadioInterface;
use radio_interface::{parse_rnode_frame, split_into_frames, SplitAssembler};
use rns_core::destination as rns_dest;
use rns_core::packet::RawPacket;
use rns_crypto::identity::Identity;
use sx126x::irq;
use sx126x::{RadioBus, Sx1262};

/// `rns_crypto::Rng` adapter over the ESP32-S3 hardware RNG (used for the
/// ephemeral X25519 key in encryption-to-server + announce random_hash).
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

// ── Inbound: log the server's reply (chart-data receive half) ───────────────
//
// The server ACKs every client message with an encrypted LXMF reply whose
// content is an `LMAOEnvelope{text: TextMessage}`; `TextMessage.content` is
// "ACK from LMAO Server — received your message (N bytes)" followed by a
// `DATA …` line carrying the recent Sprout moisture series, so the client
// charts it with no extra airtime. The decrypt + opportunistic-mirror unpack +
// chart parse all live in `leaf_lxmf`; this just prints the result.

/// Decrypt + unpack + parse the server's reply to one of our messages, then
/// log the `INBOUND` / `reply` / `CHART` lines (mirrors the stable client's
/// `handle_reply` + `chart.parse_data_line`).
fn handle_lxmf_reply(identity: &Identity, pkt: &RawPacket) {
    let Some(reply) = leaf_lxmf::decrypt_server_reply(
        identity,
        &pkt.data,
        &SERVER_PUBLIC_KEY,
        &leaf_lxmf::lxmf_delivery_hash(identity),
    ) else {
        // decrypt/unpack failed — not for us or a bad key; skip silently.
        return;
    };
    esp_println::println!(
        "[rns] INBOUND lxmf src={:02x?} title={:?} sig_valid={:?} content={} B",
        &reply.src_hash[..8],
        core::str::from_utf8(&reply.title).unwrap_or("<bin>"),
        reply.sig_valid,
        reply.content.len()
    );
    // First line of the decoded text is the ACK itself.
    for line in reply.text.lines().take(1) {
        esp_println::println!("[rns] reply: {line}");
    }
    if let Some(c) = reply.chart {
        esp_println::println!(
            "[rns] CHART node={} dry={} wet={} temp={:?} humidity={:?} samples={:?} water_mask={:#x}",
            c.node, c.dry, c.wet, c.temp, c.humidity, c.samples, c.water_mask
        );
    }
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
                        // + parse it for the piggybacked chart data.
                        if pkt.flags.packet_type == rns_core::constants::PACKET_TYPE_DATA
                            && pkt.destination_hash == leaf_lxmf::lxmf_delivery_hash(identity)
                        {
                            handle_lxmf_reply(identity, &pkt);
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
