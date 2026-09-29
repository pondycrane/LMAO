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
    pub const SET_FS: u8 = 0xC1;
    pub const SET_TX: u8 = 0x83;
    pub const SET_TX_PARAMS: u8 = 0x8E;
    pub const WRITE_BUFFER: u8 = 0x0E;
    pub const WRITE_REGISTER: u8 = 0x0D;
    pub const CALIBRATE: u8 = 0x89;
    pub const CALIBRATE_IMAGE: u8 = 0x98;
    pub const GET_DEVICE_ERRORS: u8 = 0x17;
    pub const CLEAR_DEVICE_ERRORS: u8 = 0x07;
    pub const SET_PA_RAMP: u8 = 0x94;
}

/// SX1262 LoRa registers (mirrors `sx126x.py` `_REG_*`).
pub mod reg {
    pub const LSYNCRH: u16 = 0x740;
    pub const LSYNCRL: u16 = 0x741;
    /// Modulation-quality workaround register (DS 15.1): set bit 2 before TX.
    pub const MODQUAL: u16 = 0x0889;
    /// TX clamp / OCP register — DS 15.2 "Better Resistance to Antenna
    /// Mismatch" workaround: the SX1262 reference init ORs 0x1E into it.
    pub const TX_CLAMP: u16 = 0x08D8;
}

/// SX1262 device-error flag bits (GET_DEVICE_ERRORS), DS 13.4.4.
pub mod err {
    pub const RC64K_CALIB_ERR: u16 = 1 << 0;
    pub const RC13M_CALIB_ERR: u16 = 1 << 1;
    pub const PLL_LOCK_ERR: u16 = 1 << 2;
    pub const XOSC_START_ERR: u16 = 1 << 3;
    pub const IMAGE_CALIB_ERR: u16 = 1 << 4;
    pub const RX_CALIB_ERR: u16 = 1 << 5;
    pub const TX_CALIB_ERR: u16 = 1 << 6;
    pub const ADC_CALIB_ERR: u16 = 1 << 7;
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
    /// Blocking delay. Required by the reference init sequence: hardware reset
    /// timing (1 ms low / 5 ms high) and the 15 ms TCXO-startup settle after
    /// `SET_DIO3_AS_TCXO_CTRL` (the chip does not hold BUSY for these).
    fn delay_ms(&mut self, ms: u32);
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

    /// Reset the radio (hardware RST), then STANDBY_RC. The bus owns the pulse;
    /// here we only mirror the reference's post-reset settle (chip boots to
    /// STDBY_RC).
    pub fn reset(&mut self) -> Result<(), B::Error> {
        self.bus.reset()?;
        self.bus.wait_ready()?;
        self.standby()
    }

    /// STANDBY (RC). Only used right after reset, before the TCXO is
    /// configured — the TX/RX path uses `standby_xosc` (see below).
    pub fn standby(&mut self) -> Result<(), B::Error> {
        self.cmd(cmd::SET_STANDBY, &[0x00])
    }

    /// STANDBY (XOSC) + clear IRQs — mirrors the reference `_standby()`, which
    /// every TX/RX prepare goes through. On this DIO3-TCXO board, firing
    /// SET_TX from STDBY_RC with the XOSC not yet running (and the expected
    /// XOSC_START_ERR uncleared) is the endless-TX state (chip stays in mode 6
    /// with no TX_DONE); the reference avoids it by always entering TX/RX from
    /// STDBY_XOSC.
    pub fn standby_xosc(&mut self) -> Result<(), B::Error> {
        self.cmd(cmd::SET_STANDBY, &[0x01])?;
        // XOSC settle: entering STDBY_XOSC from STDBY_RC restarts the crystal
        // (off in RC mode). On this DIO3-TCXO module the startup takes the
        // configured TCXO time (~5 ms); issuing SET_TX before the crystal is
        // stable hangs the chip in TX forever (no TX_DONE, no error flag).
        // The reference never hits this: Python command gaps are milliseconds
        // and its continuous-RX idle keeps the XOSC warm.
        self.bus.delay_ms(5);
        // XOSC_START_ERR re-latches on every RC→XOSC crystal restart; clear it
        // so a subsequent SET_TX is not rejected.
        self.clear_device_errors()?;
        self.clear_irq()
    }

    /// Clear all IRQ flags (mirrors `_clear_irq()`).
    pub fn clear_irq(&mut self) -> Result<(), B::Error> {
        self.cmd(cmd::CLR_IRQ_STATUS, &0xFFFFu16.to_be_bytes())
    }

    /// Map IRQ sources to DIO1 (mirrors `_CMD_CFG_DIO_IRQ` in the reference
    /// init): RX_DONE|TX_DONE|TIMEOUT|CRC_ERR overall, RX_DONE|TX_DONE|TIMEOUT
    /// on DIO1, nothing on DIO2/DIO3.
    pub fn set_dio_irq_masks(&mut self) -> Result<(), B::Error> {
        let mask = (irq::RX_DONE | irq::TX_DONE | irq::TIMEOUT | irq::CRC_ERR).to_be_bytes();
        let dio1 = (irq::RX_DONE | irq::TX_DONE | irq::TIMEOUT).to_be_bytes();
        self.cmd(cmd::CFG_DIO_IRQ, &[mask[0], mask[1], dio1[0], dio1[1], 0x00, 0x00, 0x00, 0x00])
    }

    /// DS 15.2 "Better Resistance of the SX1262 Tx to Antenna Mismatch"
    /// workaround (applied by the reference `_SX1262.__init__`): 0x8D8 |= 0x1E.
    pub fn antenna_mismatch_workaround(&mut self) -> Result<(), B::Error> {
        let v = self.reg_read_u8(reg::TX_CLAMP)?;
        self.reg_write_u8(reg::TX_CLAMP, v | 0x1E)
    }

    /// Enter FS mode (synthesizer on) — diagnostic: a PLL that locks reaches
    /// mode 4; a dead synthesizer never does.
    pub fn set_fs(&mut self) -> Result<(), B::Error> {
        self.cmd(cmd::SET_FS, &[])
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

    /// Write a 16-bit register (CMD_WRITE_REGISTER).
    pub fn write_register(&mut self, addr: u16, val: u16) -> Result<(), B::Error> {
        let a = addr.to_be_bytes();
        let v = val.to_be_bytes();
        self.cmd(cmd::WRITE_REGISTER, &[a[0], a[1], v[0], v[1]])
    }
    /// Read a single register byte (mirrors `_reg_read`): response lays out
    /// [status, data] → data at b[1].
    pub fn reg_read_u8(&mut self, addr: u16) -> Result<u8, B::Error> {
        let a = addr.to_be_bytes();
        let b = self.cmd_read(cmd::READ_REGISTER, &a, 2)?;
        Ok(b[1])
    }

    /// Write a single register byte (mirrors `_reg_write`: one data byte).
    /// Note the old `write_register_u8` wrote TWO bytes (clobbering addr+1) —
    /// this is the correct single-byte form.
    pub fn reg_write_u8(&mut self, addr: u16, val: u8) -> Result<(), B::Error> {
        let a = addr.to_be_bytes();
        self.cmd(cmd::WRITE_REGISTER, &[a[0], a[1], val])
    }

    /// Calibrate RC oscillators, PLL and ADC (CMD_CALIBRATE, mask 0xFE = all).
    /// The µReticulum driver runs this as part of radio bring-up; without it the
    /// PLL may not lock and TX never asserts TX_DONE.
    pub fn calibrate(&mut self) -> Result<(), B::Error> {
        self.cmd(cmd::CALIBRATE, &[0xFE])?;
        self.bus.wait_ready()
    }

    /// In-band image calibration (CMD_CALIBRATE_IMAGE) for the 868 MHz band
    /// (863–870 MHz → arg 0xD7DB). The µReticulum RF path calls this right after
    /// `calibrate()`; without it the PLL may not synthesize in-band, causing an
    /// endless-TX stuck state (mode 6, no TX_DONE).
    pub fn calibrate_image(&mut self) -> Result<(), B::Error> {
        self.cmd(cmd::CALIBRATE_IMAGE, &[0xD7, 0xDB])?;
        self.bus.wait_ready()
    }

    /// Device error word (GET_DEVICE_ERRORS, DS 13.4.4) — e.g. PLL_LOCK_ERR /
    /// XOSC_START_ERR. Fatal errors block TX/RX until cleared.
    pub fn get_device_errors(&mut self) -> Result<u16, B::Error> {
        let b = self.cmd_read(cmd::GET_DEVICE_ERRORS, &[], 2)?;
        Ok(((b[1] as u16) << 8) | b[2] as u16)
    }

    /// Clear device error flags (CLEAR_DEVICE_ERRORS). DS 13.4.3 takes a
    /// 2-byte 0x0000 argument (the reference sends it; a short frame may not
    /// execute).
    pub fn clear_device_errors(&mut self) -> Result<(), B::Error> {
        self.cmd(cmd::CLEAR_DEVICE_ERRORS, &[0x00, 0x00])
    }

    /// Set LoRa modulation params: SF, BW register code, coding-rate (4/xx), LDRO.
    pub fn set_modulation_params(&mut self, sf: u8, bw: u8, cr_denom: u8, ldro: u8) -> Result<(), B::Error> {
        self.cmd(cmd::SET_MODULATION_PARAMS, &[sf, bw, cr_denom, ldro])
    }

    /// Enable the on-chip RF switch via DIO2 (the board's `dio2_rf_sw`).
    pub fn set_dio2_as_rf_switch(&mut self) -> Result<(), B::Error> {
        self.cmd(cmd::SET_DIO2_AS_RF_SWITCH_CTRL, &[0x01])
    }

    /// Set DIO3 as TCXO control. `millivolts` (1.6–3.3 V) and `start_us` mirror
    /// the µReticulum `sx126x.py` (Cardputer: 1800 mV, 5000 us): trim is the
    /// index of the nearest table value, timeout in 15.625 us units.
    /// Valid startup is required for the XOSC/PLL to lock so TX can complete —
    /// an invalid trim (e.g. 8) breaks the clock and TX never asserts TX_DONE.
    pub fn set_dio3_as_tcxo(&mut self, millivolts: u16, start_us: u32) -> Result<(), B::Error> {
        let timeout = (start_us * 1000 + 15624) / 15625;
        let mut dv = millivolts / 100;
        let trim_lut = [16u16, 17, 18, 22, 24, 27, 30, 33];
        while !trim_lut.contains(&dv) {
            dv -= 1;
        }
        let trim = trim_lut.iter().position(|&v| v == dv).unwrap() as u8;
        let t = timeout.min(0xFFFFFF) as u32;
        // [trim, timeout_msb, timeout_mid, timeout_lsb]
        self.cmd(cmd::SET_DIO3_AS_TCXO_CTRL, &[trim, (t >> 16) as u8, (t >> 8) as u8, t as u8])?;
        // Reference: settle 15 ms for the TCXO to start, then clear the
        // *expected* XOSC_START_ERR that DS 13.3.6 says is flagged here. The
        // chip does not hold BUSY for the startup, hence a real delay.
        self.bus.delay_ms(15);
        self.clear_device_errors()
    }

    /// Set PA config + TX power/ramp, mirroring the µReticulum `_get_pa_tx_params`
    /// optimal-value table (Cardputer fixed profile = 14 dBm → `[0x02,0x02,0x00,0x01]`
    /// with a nominal `SetTxParams` power of 22). `ramp` 0x06 = 200 us.
    pub fn set_pa_config(&mut self, output_power: u8, ramp: u8) -> Result<(), B::Error> {
        let (pa, tx_power): ([u8; 4], u8) = match output_power {
            22 => ([0x04, 0x07, 0x00, 0x01], 22),
            20 => ([0x03, 0x05, 0x00, 0x01], 22),
            17 => ([0x02, 0x03, 0x00, 0x01], 22),
            14 => ([0x02, 0x02, 0x00, 0x01], 22),
            _ => ([0x04, 0x07, 0x00, 0x01], output_power & 0xFF),
        };
        self.cmd(cmd::SET_PA_CONFIG, &pa)?;
        self.cmd(cmd::SET_TX_PARAMS, &[tx_power, ramp])
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
    /// Byte-exact with the reference `prepare_send`: STDBY_XOSC + IRQ clear,
    /// packet params (preamble 24 = the leaf's fixed profile), buffer base,
    /// payload write, then the DS 15.1 modulation-quality RMW (single byte).
    pub fn prepare_send(&mut self, payload: &[u8]) -> Result<(), B::Error> {
        self.standby_xosc()?;
        self.set_packet_params(24, 0, payload.len() as u8, 1, 0)?;
        self.cmd(cmd::SET_BUFFER_BASE_ADDRESS, &[0x00, 0xFF])?;
        // CMD_WRITE_BUFFER: [offset, data...] — single CS-held transaction.
        let mut wbuf = [0u8; 256];
        wbuf[0] = 0x00; // TX offset
        wbuf[1..1 + payload.len()].copy_from_slice(payload);
        self.cmd(cmd::WRITE_BUFFER, &wbuf[..1 + payload.len()])?;
        // DS 15.1 modulation-quality workaround (BW<500kHz → set bit 2),
        // read-modify-write of the single byte — required before each TX.
        let v = self.reg_read_u8(reg::MODQUAL)?;
        self.reg_write_u8(reg::MODQUAL, v | 0x04)
    }

    /// Fire the loaded TX buffer.
    pub fn start_tx(&mut self) -> Result<(), B::Error> {
        self.start_tx_timeout([0x00, 0x00, 0x00])
    }

    /// Fire the loaded TX buffer with a 24-bit timeout (15.625 us steps).
    /// A non-zero timeout force-aborts an endless TX and raises TX_TIMEOUT,
    /// letting the radio return to standby if a frame never completes.
    pub fn start_tx_timeout(&mut self, timeout24: [u8; 3]) -> Result<(), B::Error> {
        self.cmd(cmd::SET_TX, &timeout24)
    }

    /// Arm a single RX (buffer base 0xFF). `timeout24` is big-endian 24-bit.
    pub fn start_rx(&mut self, timeout24: [u8; 3]) -> Result<(), B::Error> {
        self.standby_xosc()?;
        self.set_packet_params(24, 0, 0xFF, 1, 0)?;
        self.cmd(cmd::SET_BUFFER_BASE_ADDRESS, &[0xFF, 0x00])?;
        self.cmd(cmd::SET_RX, &timeout24)
    }

    /// IRQ status word.
    pub fn get_irq_status(&mut self) -> Result<u16, B::Error> {
        let b = self.cmd_read(cmd::GET_IRQ_STATUS, &[], 3)?;
        Ok(((b[1] as u16) << 8) | b[2] as u16)
    }

    /// Raw STATUS byte (first byte returned by any command) + its opmode nibble.
    /// Opmode in bits[5:4]: 0=STBY_RC,1=STBY_XOSC,2=FS,3=RX,4=TX.
    pub fn get_status(&mut self) -> Result<(u8, u8), B::Error> {
        let b = self.cmd_read(cmd::GET_IRQ_STATUS, &[], 3)?;
        let status = b[0];
        Ok((status, (status >> 4) & 0x7))
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
