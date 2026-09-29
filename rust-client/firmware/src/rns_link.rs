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

use radio_interface::interface::RadioInterface;
use radio_interface::{parse_rnode_frame, split_into_frames, SplitAssembler};
use sx126x::irq;
use sx126x::{RadioBus, Sx1262};

/// Longest whole RNS packet the radio carries (matching RNode: 2×254).
const MAX_PACKET: usize = 508;
/// Split-fragment reassembly timeout (ms) — mirrors `_REASM_TIMEOUT` × 1000.
const REASM_TIMEOUT_MS: u32 = 15_000;

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
            // Bounded TX: airtime ~60 ms, timeout 200 ms (0x3200 * 15.625 us).
            self.radio.start_tx_timeout([0x00, 0x32, 0x00]).map_err(|_| ())?;
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
