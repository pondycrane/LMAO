//! Cardputer RNS **Link initiator + Resource push** to the LMAO server's
//! `lmao.data` destination (issue #197).
//!
//! Establishes an RNS Link (LINKREQUEST → LRPROOF → LRRTT) against the server,
//! then pushes a big message as an RNS Resource (advertise → parts → proof),
//! exactly the flow the server consumes via `set_resource_callback` /
//! `set_resource_concluded_callback` (RNS 1.3.5 accepts a Resource **only**
//! over a Link).
//!
//! Frame-size discipline: every whole RNS packet this module emits is kept
//! ≤254 B so `split_into_frames` produces exactly one non-flagged LoRa frame
//! (a >254-B split packet's second frame does not radiate on this radio — the
//! firmware frame2 bug). The resource part SDUs are sized so each part carrier
//! stays ≤254 B.

extern crate alloc;

use alloc::vec::Vec;

use rns_core::constants::{
    CONTEXT_LRPROOF, CONTEXT_LRRTT, CONTEXT_NONE, CONTEXT_RESOURCE, CONTEXT_RESOURCE_ADV,
    CONTEXT_RESOURCE_PRF, CONTEXT_RESOURCE_REQ, DESTINATION_LINK, DESTINATION_SINGLE, HEADER_1,
    PACKET_TYPE_DATA, PACKET_TYPE_LINKREQUEST, PACKET_TYPE_PROOF,
};
use rns_core::link::crypto::create_session_token;
use rns_core::link::{LinkEngine, LinkMode};
use rns_core::packet::{PacketFlags, RawPacket};
use rns_core::resource::ResourceAction;
use rns_crypto::Rng;

use esp_println::println;
use leaf_resource::ResourceTx;

/// Server `lmao.data` destination hash — printed by the server at startup
/// ("Link/Resource destination (lmao.data DEST_HASH)"). Derives from the
/// persisted server identity + aspects `("lmao","data")`.
pub const SERVER_LMAO_DATA_HASH: [u8; 16] = [
    0x24, 0xa0, 0x97, 0x04, 0x3d, 0x6d, 0x7f, 0x8f, 0xe0, 0xfb, 0x18, 0x8e, 0x37, 0x5c, 0x4c,
    0x66,
];

/// Server Ed25519 signing pubkey = `SERVER_PUBLIC_KEY[32..64]` (X25519||Ed25519
/// identity layout). The initiator validates the LRPROOF against this.
pub const SERVER_ED25519_PUB: [u8; 32] = [
    0x78, 0x90, 0x18, 0x21, 0x3f, 0x31, 0x15, 0x23, 0x9d, 0x0e, 0xd1, 0x03, 0x6c, 0xf3, 0x37,
    0x5e, 0xe7, 0x5c, 0xd4, 0x10, 0x31, 0x8e, 0xda, 0x87, 0xfb, 0x94, 0x20, 0xce, 0x7a, 0x4c,
    0xa7, 0x88,
];

/// Link packet (19-byte HEADER_1 + `encrypt_with_iv` expansion ≈ IV16 + HMAC32
/// + CBC padding) must stay ≤ 254 B so each RF packet is a single LoRa frame
/// (a >254-B split packet's second frame does not radiate on this radio).
/// pkcs7 pads a full block when part%16==0, so for 160 B: 19 + 160 + 16 + 32 + 16 = 243 ≤ 254.
pub const RESOURCE_SDU: usize = 160;

/// Fixed IV for the resource-payload link encryption (`Token::encrypt_with_iv`
/// is the deterministic, rng-free link cipher path the no-rng `encrypt_fn`
/// requires). The IV travels with the ciphertext (`IV ‖ AES-CBC ‖ HMAC`), so
/// the responder's `Link.decrypt` reverses it regardless of the IV value.
const RESOURCE_IV: [u8; 16] = [
    0x1a, 0x2b, 0x3c, 0x4d, 0x5e, 0x6f, 0x70, 0x81, 0x92, 0xa3, 0xb4, 0xc5, 0xd6, 0xe7, 0xf8,
    0x09,
];

/// Phases of the Link+Resource state machine.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum LinkPhase {
    Idle,
    /// LINKREQUEST sent, waiting for LRPROOF.
    Linking,
    /// Handshake complete; resource may be advertised.
    Active,
    /// Resource advertised, awaiting a part request.
    Advertised,
    /// Serving part requests.
    Transferring,
    /// All parts served, awaiting/validating the server proof.
    AwaitingProof,
    Complete,
    Failed,
}

/// Link+Resource driver: holds the initiator `LinkEngine`, the established
/// link_id, and the in-flight `ResourceTx`.
pub struct LinkResource {
    pub phase: LinkPhase,
    engine: Option<LinkEngine>,
    pub link_id: Option<[u8; 16]>,
    /// Derived link session key (for link-cipher packet encryption). Copied
    /// out of the engine so packet sends can borrow other fields freely.
    session_key: Option<([u8; 64], usize)>,
    tx: Option<ResourceTx>,
    pub sent_parts: usize,
    pub total_parts: usize,
    _establish_attempts: u32,
    /// Monotonic counter to vary the per-packet IV (link cipher AES-CBC).
    packet_iv_hi: u32,
}

impl LinkResource {
    pub fn new() -> Self {
        Self {
            phase: LinkPhase::Idle,
            engine: None,
            link_id: None,
            session_key: None,
            tx: None,
            sent_parts: 0,
            total_parts: 0,
            _establish_attempts: 0,
            packet_iv_hi: 0,
        }
    }

    /// True once the handshake is complete (link usable for a Resource).
    pub fn established(&self) -> bool {
        matches!(
            self.phase,
            LinkPhase::Active
                | LinkPhase::Advertised
                | LinkPhase::Transferring
                | LinkPhase::AwaitingProof
                | LinkPhase::Complete
        )
    }

    /// Reset a stalled or failed link back to Idle so the caller can issue a
    /// fresh LINKREQUEST (a single lost LRPROOF would otherwise strand the
    /// half-duplex link in `Linking` forever).
    pub fn reset(&mut self) {
        self.phase = LinkPhase::Idle;
        self.engine = None;
        self.link_id = None;
        self.session_key = None;
        self.tx = None;
        self.sent_parts = 0;
        self.total_parts = 0;
        self.packet_iv_hi = 0;
    }

    /// Build the LINKREQUEST packet addressed to the server's `lmao.data`
    /// destination. Single frame (≤254 B). Sets the link_id from the packet's
    /// hashable part and transitions to `Linking`. Returns the raw packet to TX.
    pub fn begin_link(&mut self, now_f: f64, rng: &mut dyn Rng) -> Option<Vec<u8>> {
        if self.phase == LinkPhase::Linking {
            return None; // already in flight
        }
        let hops: u8 = 1;
        let (engine, request_data) = LinkEngine::new_initiator(
            &SERVER_LMAO_DATA_HASH,
            hops,
            LinkMode::Aes256Cbc,
            Some(500),
            now_f,
            rng,
        );
        let flags = PacketFlags {
            header_type: HEADER_1,
            context_flag: 0,
            transport_type: 0,
            destination_type: DESTINATION_SINGLE,
            packet_type: PACKET_TYPE_LINKREQUEST,
        };
        let pkt = RawPacket::pack(flags, hops, &SERVER_LMAO_DATA_HASH, None, CONTEXT_NONE, &request_data)
            .ok()?;
        let hashable = pkt.get_hashable_part();
        let mut engine = engine;
        engine.set_link_id_from_hashable(&hashable, request_data.len());
        self.link_id = Some(*engine.link_id());
        self.engine = Some(engine);
        self.phase = LinkPhase::Linking;
        self._establish_attempts = self._establish_attempts.wrapping_add(1);
        Some(pkt.raw)
    }

    /// Wrap payload in a link packet (DATA or PROOF) addressed to *link_id*.
    /// Static: called while other `self` fields are mutably borrowed.
    fn build_link_packet(
        link_id: &[u8; 16],
        packet_type: u8,
        context: u8,
        data: &[u8],
    ) -> Option<Vec<u8>> {
        let flags = PacketFlags {
            header_type: HEADER_1,
            context_flag: 0,
            transport_type: 0,
            destination_type: DESTINATION_LINK,
            packet_type,
        };
        RawPacket::pack(flags, 0, link_id, None, context, data)
            .ok()
            .map(|p| p.raw)
    }

    /// Build a link packet whose payload is **encrypted with the link cipher**
    /// (the responder's `Link.receive` calls `self.decrypt(packet.data)`, so a
    /// plaintext link DATA/ADV/part is rejected as "Token HMAC was invalid").
    /// Static: takes the session key by value so the caller can still borrow
    /// `self.tx`/`self.engine` mutably.
    fn encrypt_pkt(
        key: Option<([u8; 64], usize)>,
        iv_hi: u32,
        link_id: &[u8; 16],
        packet_type: u8,
        context: u8,
        data: &[u8],
    ) -> Option<Vec<u8>> {
        let (k, n) = key?;
        let token = create_session_token(&k[..n]).ok()?;
        // Fresh IV per packet (IV rides in the ciphertext; responder decrypts
        // with it, so value is arbitrary as long as it's unique enough).
        let mut iv = RESOURCE_IV;
        iv[..4].copy_from_slice(&iv_hi.to_be_bytes());
        let enc = token.encrypt_with_iv(data, &iv);
        Self::build_link_packet(link_id, packet_type, context, &enc)
    }

    /// Decrypt an inbound link packet payload with the session token
    /// (the responder encrypts every link DATA/REQ/PRF packet).
    fn decrypt_data(key: Option<([u8; 64], usize)>, data: &[u8]) -> Option<Vec<u8>> {
        let (k, n) = key?;
        let token = create_session_token(&k[..n]).ok()?;
        token.decrypt(data).ok()
    }

    /// Feed an inbound decoded RNS packet. Returns outbound packet payloads
    /// (each ≤254 B single frames) for the caller to TX via `link.send`.
    ///
    /// Handles: LRPROOF → validate + send LRRTT (handshake completes); link
    /// resource part-requests → serve parts; resource proof → finalize.
    pub fn pump_inbound(&mut self, pkt: &RawPacket, now_f: f64, rng: &mut dyn Rng) -> Vec<Vec<u8>> {
        let mut out = Vec::new();
        let Some(link_id) = self.link_id else {
            return out;
        };
        if pkt.destination_hash != link_id {
            return out;
        }
        match pkt.flags.packet_type {
            PACKET_TYPE_PROOF if pkt.context == CONTEXT_LRPROOF => {
                if let Some(engine) = self.engine.as_mut() {
                    match engine.handle_lrproof(&pkt.data, &SERVER_ED25519_PUB, now_f, rng) {
                        Ok((lrrtt_enc, _actions)) => {
                            self.phase = LinkPhase::Active;
                            // Persist the derived session key for link-cipher
                            // packet encryption (advertisement/parts).
                            if let Some(dk) = engine.derived_key() {
                                let n = dk.len().min(64);
                                let mut k = [0u8; 64];
                                k[..n].copy_from_slice(&dk[..n]);
                                self.session_key = Some((k, n));
                            }
                            if let Some(lrrtt) =
                                Self::build_link_packet(&link_id, PACKET_TYPE_DATA, CONTEXT_LRRTT, &lrrtt_enc)
                            {
                                out.push(lrrtt);
                            }
                        }
                        Err(_) => self.phase = LinkPhase::Failed,
                    }
                }
            }
            PACKET_TYPE_DATA => match pkt.context {
                CONTEXT_RESOURCE_REQ => {
                    println!(
                        "[dbg] REQ pkt received ctlen={} ctx={}",
                        pkt.data.len(),
                        pkt.context
                    );
                    let Some(req) = Self::decrypt_data(self.session_key, &pkt.data) else {
                        println!("[dbg] REQ decrypt FAILED");
                        return out;
                    };
                    println!(
                        "[dbg] REQ decrypted {}B f0-4={:02x}{:02x}{:02x}{:02x}{:02x}",
                        req.len(),
                        req[0],
                        req[1],
                        req[2],
                        req[3],
                        req[4]
                    );
                    let mut parts = 0usize;
                    if let Some(tx) = self.tx.as_mut() {
                        for a in tx.handle_request(&req, now_f) {
                            if let ResourceAction::SendPart(p) = a {
                                parts += 1;
                                // Parts are already pre-encrypted by
                                // ResourceSender's `encrypt_fn`; rns-net carries
                                // CONTEXT_RESOURCE raw (it does NOT
                                // link-decrypt parts, unlike ADV/REQ/HMU/PRF),
                                // so wrap them in a plain link DATA packet.
                                if let Some(pp) = Self::build_link_packet(
                                    &link_id,
                                    PACKET_TYPE_DATA,
                                    CONTEXT_RESOURCE,
                                    &p,
                                ) {
                                    out.push(pp);
                                }
                            }
                        }
                    }
                    println!("[dbg] REQ served parts={}", parts);
                }
                CONTEXT_RESOURCE_PRF => {
                    let Some(proof) = Self::decrypt_data(self.session_key, &pkt.data) else {
                        return out;
                    };
                    if let Some(tx) = self.tx.as_mut() {
                        for a in tx.handle_proof(&proof, now_f) {
                            if let ResourceAction::Completed = a {
                                self.phase = LinkPhase::Complete;
                            }
                        }
                    }
                }
                _ => {}
            },
            _ => {}
        }
        out
    }

    /// Re-advertise the in-flight resource (each awake tick until the server
    /// requests parts). Returns advertisement packets to TX.
    pub fn poll_resource(&mut self, now_f: f64) -> Vec<Vec<u8>> {
        let mut out = Vec::new();
        if !self.established() {
            return out;
        }
        let Some(link_id) = self.link_id else {
            return out;
        };
        let key = self.session_key;
        let mut iv = self.packet_iv_hi;
        self.packet_iv_hi = iv.wrapping_add(1024);
        let Some(tx) = self.tx.as_mut() else {
            return out;
        };
        for a in tx.advertise(now_f) {
            if let ResourceAction::SendAdvertisement(adv) = a {
                if let Some(p) =
                    Self::encrypt_pkt(key, iv, &link_id, PACKET_TYPE_DATA, CONTEXT_RESOURCE_ADV, &adv)
                {
                    out.push(p);
                }
                iv = iv.wrapping_add(1);
            }
        }
        let (sent, total) = tx.progress();
        self.sent_parts = sent;
        self.total_parts = total;
        out
    }

    /// Start pushing `data` as a Resource over the established link. The whole
    /// payload is encrypted once with the link cipher (deterministic token
    /// path, no rng — matching the resource `encrypt_fn` contract), split into
    /// parts, and advertised for the server to request. Returns packets to TX
    /// (call repeatedly via `poll_resource` on subsequent ticks).
    pub fn start_resource(&mut self, data: &[u8], now_f: f64) -> Vec<Vec<u8>> {
        // Extract the token + rtt as locals first so the encrypt closure does
        // not borrow self (ResourceSender applies encrypt_fn once during new()).
        let (token, rtt) = {
            let Some(engine) = self.engine.as_ref() else {
                return Vec::new();
            };
            let Some(dk) = engine.derived_key() else {
                return Vec::new();
            };
            // Aes256Cbc derives a 64-byte session key (32→AES-128, 64→AES-256).
            let mut dk_arr = [0u8; 64];
            let n = dk.len().min(64);
            dk_arr[..n].copy_from_slice(&dk[..n]);
            let Some(token) = create_session_token(&dk_arr[..n]).ok() else {
                return Vec::new();
            };
            (token, engine.rtt().unwrap_or(0.1))
        };
        if !self.established() {
            return Vec::new();
        }
        let Some(link_id) = self.link_id else {
            return Vec::new();
        };

        let encrypt_fn = |pt: &[u8]| -> Vec<u8> { token.encrypt_with_iv(pt, &RESOURCE_IV) };
        let mut rng_es = EspFixedRng::default();
        let mut tx = match ResourceTx::new(
            data,
            None,
            RESOURCE_SDU,
            &encrypt_fn,
            &mut rng_es,
            now_f,
            rtt,
        ) {
            Ok(t) => t,
            Err(_) => return Vec::new(),
        };
        let mut out = Vec::new();
        let key = self.session_key;
        let mut iv = self.packet_iv_hi;
        self.packet_iv_hi = iv.wrapping_add(1024);
        for a in tx.advertise(now_f) {
            if let ResourceAction::SendAdvertisement(adv) = a {
                if let Some(p) =
                    Self::encrypt_pkt(key, iv, &link_id, PACKET_TYPE_DATA, CONTEXT_RESOURCE_ADV, &adv)
                {
                    out.push(p);
                }
                iv = iv.wrapping_add(1);
            }
        }
        let (sent, total) = tx.progress();
        self.sent_parts = sent;
        self.total_parts = total;
        self.tx = Some(tx);
        self.phase = LinkPhase::Advertised;
        out
    }
}

/// Minimal no_std `Rng` used to satisfy `ResourceSender::new`'s rng argument
/// (the resource encryption is deterministic via `encrypt_with_iv`, so this
/// rng is never actually consumed for the payload cipher).
#[derive(Default)]
struct EspFixedRng(u32);

impl Rng for EspFixedRng {
    fn fill_bytes(&mut self, dest: &mut [u8]) {
        let mut v = self.0.wrapping_mul(1_664_525) + 1_013_904_223;
        for b in dest.iter_mut() {
            v = v.wrapping_mul(1_103_515_245).wrapping_add(12_345);
            *b = (v >> 16) as u8;
        }
        self.0 = v;
    }
}
