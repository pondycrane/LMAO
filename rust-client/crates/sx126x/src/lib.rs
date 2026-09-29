//! no_std SX1262 LoRa driver core (RF-leg, ticket T3) — bus-generic.
//!
//! Faithful port of the repo's MicroPython `cardputer_client/lib/lora/sx126x.py`
//! (the exact driver that RF-works on this Cardputer): the SPI command /
//! register encodings and the chip-register calculations are ported verbatim,
//! so the leaf's fixed RF profile (868 / BW125 / SF7 / CR4:5 / pre24 /
//! syncword 0x1424) writes the same bytes the reference writes on-air.
//!
//! The SPI byte-exchange + BUSY/data lines sit behind the `RadioBus` trait so
//! the driver core is fully **host-testable** (a mock bus records every
//! command) and the esp-hal SPI binding is a thin `RadioBus` impl. RF RF TX/RX
//! correctness is the on-hardware leg; the *encodings* are locked here.
//!
//! `no_std`, `alloc`-free (all SPI command buffers are small stack arrays).

#![no_std]





/// SX1262 opcodes (mirrors `sx126x.py` `_CMD_*`).
pub mod cmd {
    pub const CFG_DIO_IRQ: u8 = 0x08;
    pub const CLR_IRQ_STATUS: u8 = 0x02;
    pub const GET_IRQ_STATUS: u8 = 0x12;
    pub const GET_RX_BUFFER_STATUS: u8 = 0x13;
    pub const GET_PACKET_STATUS: u8 = 0x14;
    pub const READ_REGISTER: u8 = 0x1D;
    pub const READ_BUFFER: u8 = 0x1E;
    pub const SET_BUFFER_BASE_ADDRESS: u8 = 0x8F;
    pub const SET_MODULATION_PARAMS: u8 = 0x8B;
    pub const SET_PACKET_PARAMS: u8 = 0x8C;
    pub const SET_PACKET_TYPE: u8 = 0x8A;
    pub const SET_PA_CONFIG: u8 = 0x95;
    pub const SET_RF_FREQUENCY: u8 = 0x86;
    pub const SET_RX: u8 = 0x82;
    pub const SET_STANDBY: u8 = 0x80;
    pub const SET_DIO3_AS_TCXO_CTRL: u8 = 0x97;
    pub const SET_DIO2_AS_RF_SWITCH_CTRL: u8 = 0x9D;
    pub const SET_TX: u8 = 0x83;
    pub const SET_TX_PARAMS: u8 = 0x8E;
    pub const WRITE_BUFFER: u8 = 0x0E;
    pub const WRITE_REGISTER: u8 = 0x0D;
}

/// SX1262 LoRa registers (mirrors `sx126x.py` `_REG_*`).
pub mod reg {
    pub const LSYNCRH: u16 = 0x740;
    pub const LSYNCRL: u16 = 0x741;
}

/// Packet types for `SET_PACKET_TYPE`.
pub mod pkt {
    pub const LORA: u8 = 0x01;
}

/// LoRa IRQ flag bits (mirrors `sx126x.py` `_IRQ_*`).
pub mod irq {
    pub const TX_DONE: u16 = 1 << 0;
    pub const RX_DONE: u16 = 1 << 1;
    pub const HEADER_ERR: u16 = 1 << 5;
    pub const CRC_ERR: u16 = 1 << 6;
    pub const TIMEOUT: u16 = 1 << 9;
    /// A successful receive: RX_DONE set, and none of timeout/crc/header err.
    pub const RX_SUCCESS_MASK: u16 = RX_DONE | TIMEOUT | CRC_ERR | HEADER_ERR;
    /// fn true when an IRQ word signals a successful receive.
    pub fn rx_success(flags: u16) -> bool {
        flags & RX_SUCCESS_MASK == RX_DONE
    }
}

/// The low-level SPI/GPIO bus a `Sx1262` talks over. Host tests use a mock;
/// the firmware supplies an esp-hal SPI impl.
pub trait RadioBus {
    type Error;
    /// Assert NSS, transmit `write` (prefixed with `opcode` by the driver),
    /// then read `read.len()` response bytes.
    fn command(&mut self, opcode: u8, write: &[u8], read: &mut [u8]) -> Result<(), Self::Error>;
    /// Assert the radio RESET line, then wait ready.
    fn reset(&mut self) -> Result<(), Self::Error>;
    /// Wait (bounded) until the BUSY line clears.
    fn wait_ready(&mut self) -> Result<(), Self::Error>;
}

/// SX1262 driver over any `RadioBus`. A leaf owns exactly one instance.
pub struct Sx1262<B: RadioBus> {
    bus: B,
}

impl<B: RadioBus> Sx1262<B> {
    pub fn new(bus: B) -> Self {
        Sx1262 { bus }
    }
    pub fn into_bus(self) -> B {
        self.bus
    }

    fn cmd(&mut self, opcode: u8, write: &[u8]) -> Result<(), B::Error> {
        self.bus.wait_ready()?;
        self.bus.command(opcode, write, &mut [])
    }
    fn cmd_read(&mut self, opcode: u8, write: &[u8], read: u8) -> Result<[u8; 256], B::Error> {
        self.bus.wait_ready()?;
        let mut buf = [0u8; 256];
        let n = (read as usize).min(buf.len());
        self.bus.command(opcode, write, &mut buf[..n])?;
        Ok(buf)
    }

    /// Reset the radio (hardware RST), then STANDBY.
    pub fn reset(&mut self) -> Result<(), B::Error> {
        self.bus.reset()?;
        self.bus.wait_ready()?;
        self.standby()
    }

    /// STANDBY (RC).
    pub fn standby(&mut self) -> Result<(), B::Error> {
        self.cmd(cmd::SET_STANDBY, &[0x00])
    }

    /// Set packet type to LoRa.
    pub fn set_packet_type_lora(&mut self) -> Result<(), B::Error> {
        self.cmd(cmd::SET_PACKET_TYPE, &[pkt::LORA])
    }

    /// Set RF frequency in Hz (`rffreq = (hz << 25) // 32_000_000`, big-endian).
    pub fn set_rf_frequency(&mut self, hz: u64) -> Result<(), B::Error> {
        let rffreq = ((hz << 25) / 32_000_000) as u32;
        self.cmd(cmd::SET_RF_FREQUENCY, &rffreq.to_be_bytes())
    }

    /// Set the LoRa sync word, applying the SX127x→SX126x nibble transform
    /// (`0xYZ -> 0xY4Z4`) when it fits in a byte.
    pub fn set_sync_word(&mut self, sw: u16) -> Result<(), B::Error> {
        let sw = if sw < 0x100 {
            0x0404 + ((sw & 0x0F) << 4) + ((sw & 0xF0) << 8)
        } else {
            sw
        };
        let b = sw.to_be_bytes();
        // CMD_WRITE_REGISTER(0x740) = [opcode, 0x07, 0x40, lo, hi]
        self.cmd(cmd::WRITE_REGISTER, &[0x07, 0x40, b[0], b[1]])
    }

    /// Read a 16-bit register (CMD_READ_REGISTER). `wr=[addr_msb, addr_lsb]`;
    /// response lays out as [status, reg_msb, reg_lsb] → data at b[1..2].
    pub fn read_register(&mut self, addr: u16) -> Result<u16, B::Error> {
        let a = addr.to_be_bytes();
        let b = self.cmd_read(cmd::READ_REGISTER, &a, 3)?;
        Ok(((b[1] as u16) << 8) | b[2] as u16)
    }

    /// Set LoRa modulation params: SF, BW register code, coding-rate (4/xx), LDRO.
    pub fn set_modulation_params(&mut self, sf: u8, bw: u8, cr_denom: u8, ldro: u8) -> Result<(), B::Error> {
        self.cmd(cmd::SET_MODULATION_PARAMS, &[sf, bw, cr_denom, ldro])
    }

    /// Enable the on-chip RF switch via DIO2 (the board's `dio2_rf_sw`).
    pub fn set_dio2_as_rf_switch(&mut self) -> Result<(), B::Error> {
        self.cmd(cmd::SET_DIO2_AS_RF_SWITCH_CTRL, &[0x01])
    }

    /// Set DIO3 as TCXO control (board 1.8V TCXO, ~5 ms start).
    pub fn set_dio3_as_tcxo(&mut self) -> Result<(), B::Error> {
        self.cmd(cmd::SET_DIO3_AS_TCXO_CTRL, &[0x08, 0x00, 0x01, 0x88])
    }

    /// Set PA config + TX power/ramp (14 dBm, 200 us ramp).
    pub fn set_pa_config(&mut self, power_dbm: u8, ramp: u8) -> Result<(), B::Error> {
        self.cmd(cmd::SET_PA_CONFIG, &[0x04, 0x07, 0x00, 0x01])?;
        self.cmd(cmd::SET_TX_PARAMS, &[power_dbm, ramp])
    }

    /// Set LoRa packet params (preamble, implicit header, payload len, CRC,
    /// invert-IQ) — must agree between TX and RX.
    pub fn set_packet_params(
        &mut self,
        preamble: u16,
        implicit: u8,
        payload_len: u8,
        crc: u8,
        invert_iq: u8,
    ) -> Result<(), B::Error> {
        let p = preamble.to_be_bytes();
        self.cmd(cmd::SET_PACKET_PARAMS, &[p[0], p[1], implicit, payload_len, crc, invert_iq])
    }

    /// Load a packet into the TX FIFO (buffer base 0x0), ready to `start_tx`.
    pub fn prepare_send(&mut self, payload: &[u8]) -> Result<(), B::Error> {
        self.standby()?;
        self.set_packet_params(8, 0, payload.len() as u8, 1, 0)?;
        self.cmd(cmd::SET_BUFFER_BASE_ADDRESS, &[0x00, 0xFF])?;
        // CMD_WRITE_BUFFER: [offset, data...] — single CS-held transaction.
        let mut wbuf = [0u8; 256];
        wbuf[0] = 0x00; // TX offset
        wbuf[1..1 + payload.len()].copy_from_slice(payload);
        self.cmd(cmd::WRITE_BUFFER, &wbuf[..1 + payload.len()])
    }

    /// Fire the loaded TX buffer.
    pub fn start_tx(&mut self) -> Result<(), B::Error> {
        self.cmd(cmd::SET_TX, &[0x00, 0x00, 0x00])
    }

    /// Arm a single RX (buffer base 0xFF). `timeout24` is big-endian 24-bit.
    pub fn start_rx(&mut self, timeout24: [u8; 3]) -> Result<(), B::Error> {
        self.standby()?;
        self.set_packet_params(8, 0, 0xFF, 1, 0)?;
        self.cmd(cmd::SET_BUFFER_BASE_ADDRESS, &[0xFF, 0x00])?;
        self.cmd(cmd::SET_RX, &timeout24)
    }

    /// IRQ status word.
    pub fn get_irq_status(&mut self) -> Result<u16, B::Error> {
        let b = self.cmd_read(cmd::GET_IRQ_STATUS, &[], 3)?;
        Ok(((b[1] as u16) << 8) | b[2] as u16)
    }

    /// `(rx_length, rx_buffer_ptr)` of a received frame.
    pub fn get_rx_buffer_status(&mut self) -> Result<(u8, u8), B::Error> {
        let b = self.cmd_read(cmd::GET_RX_BUFFER_STATUS, &[], 3)?;
        Ok((b[1], b[2]))
    }

    /// Signal quality of the last received frame: `(rssi_dbm, snr_db)` — the
    /// leaf's RSSI source (sensor_id 9 `SensorReport`).
    pub fn get_packet_status(&mut self) -> Result<(i16, f32), B::Error> {
        let b = self.cmd_read(cmd::GET_PACKET_STATUS, &[], 4)?;
        let rssi = b[1] as i16 / -2; // dBm (reference: `rssi //= -2`)
        let snr = (b[2] as i8) as f32 / 4.0; // dB (`snr = s8 / 4`)
        Ok((rssi, snr))
    }

    /// Recover a received frame into `out`. Returns bytes copied.
    pub fn read_buffer(&mut self, ptr: u8, out: &mut [u8]) -> Result<usize, B::Error> {
        let n = out.len().min(255);
        let b = self.cmd_read(cmd::READ_BUFFER, &[ptr], (n + 1) as u8)?;
        out[..n].copy_from_slice(&b[1..1 + n]);
        Ok(n)
    }
}
