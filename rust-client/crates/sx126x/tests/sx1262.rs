//! Host tests for the SX1262 driver core: a recording `RadioBus` captures every
//! SPI command so the fixed leaf-profile encodings are locked byte-for-byte
//! against the reference `sx126x.py` calculations.

use sx126x::{cmd, reg, RadioBus, Sx1262};

/// A bus that records every command dispatched, with canned read responses.
#[derive(Default)]
struct RecordingBus {
    commands: Vec<(u8, Vec<u8>)>,
    /// opcode -> read response bytes (byte 0 = status).
    reads: Vec<(u8, Vec<u8>)>,
    /// delay_ms calls recorded (ms values).
    delays: Vec<u32>,
}

impl RadioBus for RecordingBus {
    type Error = ();
    fn command(&mut self, opcode: u8, write: &[u8], read: &mut [u8]) -> Result<(), ()> {
        self.commands.push((opcode, write.to_vec()));
        if let Some((_, resp)) = self.reads.iter().find(|(o, _)| *o == opcode) {
            let n = read.len().min(resp.len());
            read[..n].copy_from_slice(&resp[..n]);
        }
        Ok(())
    }
    fn reset(&mut self) -> Result<(), ()> {
        self.commands.push((0xFE, Vec::new()));
        Ok(())
    }
    fn wait_ready(&mut self) -> Result<(), ()> {
        Ok(())
    }
    fn delay_ms(&mut self, ms: u32) {
        self.delays.push(ms);
    }
}

/// Drive a fresh driver over a fresh bus (with canned reads) and return its
/// recorded command stream — the golden byte lock.
fn log(reads: Vec<(u8, Vec<u8>)>, f: impl FnOnce(&mut Sx1262<RecordingBus>)) -> Vec<(u8, Vec<u8>)> {
    let bus = RecordingBus { commands: Vec::new(), reads, delays: Vec::new() };
    let mut radio = Sx1262::new(bus);
    f(&mut radio);
    radio.into_bus().commands
}

#[test]
fn rf_frequency_868mhz_golden() {
    // rffreq = (868e6 << 25) // 32e6 = 0x36400000 (reference formula).
    let cmds = log(vec![], |r| r.set_rf_frequency(868_000_000).unwrap());
    assert_eq!(cmds, vec![(cmd::SET_RF_FREQUENCY, vec![0x36, 0x40, 0x00, 0x00])]);
}

#[test]
fn sync_word_0x1424_written_verbatim_to_reg() {
    // The leaf's syncword (0x1424) is already the 16-bit SX1262 form, so no
    // nibble transform applies (that is only for sw < 0x100).
    let cmds = log(vec![], |r| r.set_sync_word(0x1424).unwrap());
    assert_eq!(
        cmds,
        vec![(cmd::WRITE_REGISTER, vec![0x07, 0x40, 0x14, 0x24])] // LSYNCRH @0x740
    );
    // A byte syncword is transformed: 0x71 -> 0x7474.
    let cmds = log(vec![], |r| r.set_sync_word(0x71).unwrap());
    assert_eq!(cmds, vec![(cmd::WRITE_REGISTER, vec![0x07, 0x40, 0x74, 0x14])]);
}

#[test]
fn modulation_sf7_bw125_cr45_golden() {
    // SF=7, BW "125" -> 0x04, CR 4/5 -> cr_denom 1 (reference: cr-4), LDRO 0.
    let cmds = log(vec![], |r| r.set_modulation_params(7, 0x04, 1, 0).unwrap());
    assert_eq!(cmds, vec![(cmd::SET_MODULATION_PARAMS, vec![0x07, 0x04, 0x01, 0x00])]);
}

#[test]
fn tcxo_config_mirrors_reference_with_settle_and_error_clear() {
    // Cardputer: 1800 mV / 5000 us. trim LUT: dv=18 -> index 2 (0x02);
    // timeout = (5000*1000 + 15624)/15625 = 320 = 0x000140. The reference then
    // sleeps 15 ms (TCXO start) and clears the expected XOSC_START_ERR.
    let bus = RecordingBus { commands: Vec::new(), reads: Vec::new(), delays: Vec::new() };
    let mut radio = Sx1262::new(bus);
    radio.set_dio3_as_tcxo(1800, 5000).unwrap();
    let bus = radio.into_bus();
    assert_eq!(
        bus.commands,
        vec![
            (cmd::SET_DIO3_AS_TCXO_CTRL, vec![0x02, 0x00, 0x01, 0x40]),
            (cmd::CLEAR_DEVICE_ERRORS, vec![0x00, 0x00]),
        ]
    );
    assert_eq!(bus.delays, vec![15]);
}

#[test]
fn pa_config_14dbm_optimal_values() {
    // 14 dBm: PA config [0x02,0x02,0x00,0x01], nominal SetTxParams power 22.
    let cmds = log(vec![], |r| r.set_pa_config(14, 0x02).unwrap());
    assert_eq!(
        cmds,
        vec![
            (cmd::SET_PA_CONFIG, vec![0x02, 0x02, 0x00, 0x01]),
            (cmd::SET_TX_PARAMS, vec![22, 0x02]),
        ]
    );
}

#[test]
fn standby_xosc_clears_irq() {
    // STDBY_XOSC (1), XOSC settle delay, clear device errors, CLR_IRQ(0xFFFF).
    let cmds = log(vec![], |r| r.standby_xosc().unwrap());
    assert_eq!(
        cmds,
        vec![
            (cmd::SET_STANDBY, vec![0x01]),
            (cmd::CLEAR_DEVICE_ERRORS, vec![0x00, 0x00]),
            (cmd::CLR_IRQ_STATUS, vec![0xFF, 0xFF]),
        ]
    );
}

#[test]
fn tx_path_loads_packet_byte_exact_to_reference() {
    // A T3-framed control frame (header 0xcb + payload) going out on the radio.
    // prepare_send: STDBY_XOSC+CLR_IRQ, packet params (pre24), buffer base,
    // write buffer, then single-byte 0x0889 read-modify-write (DS 15.1).
    let frame: Vec<u8> = vec![0xcb, 0x01, 0x02, 0x03, 0x04];
    let a = reg::MODQUAL.to_be_bytes();
    let cmds = log(vec![(cmd::READ_REGISTER, vec![0x00, 0x00])], |r| {
        r.prepare_send(&frame).unwrap();
        r.start_tx().unwrap();
    });
    assert_eq!(
        cmds,
        vec![
            (cmd::SET_STANDBY, vec![0x01]),
            (cmd::CLEAR_DEVICE_ERRORS, vec![0x00, 0x00]),
            (cmd::CLR_IRQ_STATUS, vec![0xFF, 0xFF]),
            // set_packet_params(preamble=24, 0, len=5, crc=1, invert=0)
            (cmd::SET_PACKET_PARAMS, vec![0x00, 0x18, 0x00, 0x05, 0x01, 0x00]),
            (cmd::SET_BUFFER_BASE_ADDRESS, vec![0x00, 0xFF]),
            (cmd::WRITE_BUFFER, vec![0x00, 0xcb, 0x01, 0x02, 0x03, 0x04]),
            (cmd::READ_REGISTER, vec![a[0], a[1]]),
            (cmd::WRITE_REGISTER, vec![a[0], a[1], 0x04]),
            (cmd::SET_TX, vec![0x00, 0x00, 0x00]),
        ]
    );
}

#[test]
fn single_byte_register_ops_do_not_clobber_neighbour() {
    // reg_write_u8 must emit exactly ONE data byte (the old helper wrote two,
    // zeroing addr+1 — a real on-device bug).
    let cmds = log(vec![], |r| r.reg_write_u8(reg::MODQUAL, 0x04).unwrap());
    assert_eq!(cmds, vec![(cmd::WRITE_REGISTER, vec![0x08, 0x89, 0x04])]);
    // reg_read_u8 picks the data byte after status.
    let cmds = log(vec![(cmd::READ_REGISTER, vec![0x62, 0xAB])], |r| {
        let v = r.reg_read_u8(reg::TX_CLAMP).unwrap();
        assert_eq!(v, 0xAB);
    });
    assert_eq!(cmds, vec![(cmd::READ_REGISTER, vec![0x08, 0xD8])]);
}

#[test]
fn antenna_mismatch_workaround_ormask() {
    // DS 15.2: 0x8D8 |= 0x1E (canned read 0x18 -> write 0x1E).
    let cmds = log(vec![(cmd::READ_REGISTER, vec![0x00, 0x18])], |r| {
        r.antenna_mismatch_workaround().unwrap()
    });
    assert_eq!(
        cmds,
        vec![
            (cmd::READ_REGISTER, vec![0x08, 0xD8]),
            (cmd::WRITE_REGISTER, vec![0x08, 0xD8, 0x1E]),
        ]
    );
}

#[test]
fn dio_irq_masks_match_reference() {
    let cmds = log(vec![], |r| r.set_dio_irq_masks().unwrap());
    // mask = RX_DONE|TX_DONE|TIMEOUT|CRC_ERR = 0x243; dio1 = RX_DONE|TX_DONE|TIMEOUT = 0x203.
    assert_eq!(
        cmds,
        vec![(cmd::CFG_DIO_IRQ, vec![0x02, 0x43, 0x02, 0x03, 0x00, 0x00, 0x00, 0x00])]
    );
}

#[test]
fn irq_receive_success_judgement() {
    use sx126x::irq;
    assert!(irq::rx_success(irq::RX_DONE));
    assert!(!irq::rx_success(irq::RX_DONE | irq::CRC_ERR));
    assert!(!irq::rx_success(irq::TIMEOUT));
}

#[test]
fn rssi_and_packet_status_decode() {
    // Canned GET_PACKET_STATUS: [status, rssi=126, snr=+30(8bit signed), pad].
    // Reference: rssi//=-2 -> -63 dBm; snr = s8/4 = 7.5 dB.
    let cmds = log(vec![(cmd::GET_PACKET_STATUS, vec![0, 126, 30, 0])], |r| {
        let (rssi, snr) = r.get_packet_status().unwrap();
        assert_eq!(rssi, -63);
        assert!((snr - 7.5).abs() < 1e-4);
    });
    assert_eq!(cmds, vec![(cmd::GET_PACKET_STATUS, Vec::<u8>::new())]);
}
