//! The leaf radio `Interface` — the µReticulum `Interface` contract transcribed
//! (from `cardputer_client/lib/urns/interfaces/__init__.py`), kept lean and
//! bound to `rns-core`'s `RawPacket` decode. This is the "clone only the
//! interface" decision (design §5: `radio-interface` is the `rns_core::Interface`):
//! the std mesh-router interface machinery (`rns-net` — threads, sockets,
//! workers) is NOT pulled onto the leaf; we keep the interface contract the
//! transport needs (flow metadata + inbound/outbound frame flow) and the raw
//! RNS packet decode it hands the protocol core.
//!
//! `no_std` + `alloc` (only for the interface name).

use alloc::borrow::ToOwned;
use alloc::string::String;

use rns_core::packet::{PacketError, RawPacket};

/// Interface modes — mirrors µReticulum `Interface.MODE_*`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum InterfaceMode {
    Full = 0x01,
    PointToPoint = 0x02,
    AccessPoint = 0x03,
    Roaming = 0x04,
    Boundary = 0x05,
    Gateway = 0x06,
}

/// Flow counters (transcribed from the reference `Interface`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct FlowStats {
    /// Bytes received.
    pub rx_bytes: u64,
    /// Bytes sent.
    pub tx_bytes: u64,
    /// Frames received.
    pub rx_frames: u64,
    /// Frames sent.
    pub tx_frames: u64,
    /// Monotonic ms of the last inbound frame.
    pub last_activity_ms: u64,
}

/// The leaf's radio `Interface`: metadata + frame flow. One instance per radio
/// (a leaf carries exactly one LoRa radio → single interface, no routing table).
#[derive(Debug, Clone)]
pub struct RadioInterface {
    pub name: String,
    pub online: bool,
    pub enabled: bool,
    pub mode: InterfaceMode,
    /// Interface link rate in bps (0 = unknown, like the reference).
    pub bitrate: u32,
    /// MTU in bytes (500, like the reference default).
    pub mtu: usize,
    /// Largest raw RNS packet this radio can carry (used for link-MTU clamping).
    pub hw_mtu: usize,
    /// Permits outbound transmissions.
    pub out: bool,
    /// Permits inbound receptions.
    pub inn: bool,
    /// Signal: RSSI in dBm (0.25 dB units per SX1262), - = weaker.
    pub rssi: Option<i16>,
    /// Signal: SNR in dB.
    pub snr: Option<f32>,
    pub stats: FlowStats,
}

impl Default for RadioInterface {
    /// The reference `Interface.__init__` defaults (µReticulum leaf).
    fn default() -> Self {
        RadioInterface {
            name: String::new(),
            online: false,
            enabled: true,
            mode: InterfaceMode::Full,
            bitrate: 0,
            mtu: 500,
            hw_mtu: 500,
            out: true,
            inn: true,
            rssi: None,
            snr: None,
            stats: FlowStats::default(),
        }
    }
}

impl RadioInterface {
    pub fn new(name: &str) -> Self {
        RadioInterface {
            name: name.to_owned(),
            ..Self::default()
        }
    }

    /// An inbound radio frame has arrived (`process_incoming`): tally flow
    /// stats, decode the raw RNS packet, and **stamp the interface's signal on
    /// it** — the µReticulum behavior (`transport.py`: `packet.rssi =
    /// interface.rssi`), which is what feeds the leaf's RSSI SensorReport.
    pub fn process_incoming(&mut self, data: &[u8], now_ms: u64) -> Result<RawPacket, PacketError> {
        self.stats.rx_bytes += data.len() as u64;
        self.stats.rx_frames += 1;
        self.stats.last_activity_ms = now_ms;

        let mut pkt = RawPacket::unpack(data)?;
        pkt.rssi = self.rssi;
        pkt.snr = self.snr;
        Ok(pkt)
    }

    /// An outbound frame was handed to the radio: tally it. The actual LoRa
    /// transmission is the firmware SX1262 driver's job (implements this flow).
    pub fn note_outgoing(&mut self, data: &[u8]) {
        self.stats.tx_bytes += data.len() as u64;
        self.stats.tx_frames += 1;
    }

    /// The leaf is offline for this interface (radio shutdown/detach).
    pub fn close(&mut self) {
        self.online = false;
        self.enabled = false;
    }
}
