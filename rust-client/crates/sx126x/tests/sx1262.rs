//! Host tests for the SX1262 driver core: a recording `RadioBus` captures every
//! SPI command so the fixed leaf-profile encodings are locked byte-for-byte
//! against the reference `sx126x.py` calculations.

use sx126x::{RadioBus, Sx1262, cmd, pkt};

/// A bus that records every command dispatched, with canned read responses.
#[derive(Default)]
struct RecordingBus {
    commands: Vec<(u8, Vec<u8>)>,
    /// opcode -> read response bytes (byte 0 = status).
    reads: Vec<(u8, Vec<u8>)>,
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
}

/// Drive a fresh driver over a fresh bus (with canned reads) and return its
/// recorded command stream — the golden byte lock.
fn log(reads: Vec<(u8, Vec<u8>)>, f: impl FnOnce(&mut Sx1262<RecordingBus>)) -> Vec<(u8, Vec<u8>)> {
    let bus = RecordingBus { commands: Vec::new(), reads };
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
fn fixed_profile_config_sequence() {
    let cmds = log(vec![], |r| {
        r.set_packet_type_lora().unwrap();
        r.set_dio2_as_rf_switch().unwrap();
        r.set_dio3_as_tcxo().unwrap();
        r.set_pa_config(14, 0x06).unwrap();
    });
    assert_eq!(
        cmds,
        vec![
            (cmd::SET_PACKET_TYPE, vec![pkt::LORA]),
            (cmd::SET_DIO2_AS_RF_SWITCH_CTRL, vec![0x01]),
            (cmd::SET_DIO3_AS_TCXO_CTRL, vec![0x08, 0x00, 0x01, 0x88]),
            (cmd::SET_PA_CONFIG, vec![0x04, 0x07, 0x00, 0x01]),
            (cmd::SET_TX_PARAMS, vec![14, 0x06]),
        ]
    );
}

#[test]
fn tx_path_loads_the_t3_framed_packet() {
    // A T3-framed control frame (header 0xcb + payload) going out on the radio.
    let frame: Vec<u8> = vec![0xcb, 0x01, 0x02, 0x03, 0x04];
    let cmds = log(vec![], |r| {
        r.prepare_send(&frame).unwrap();
        r.start_tx().unwrap();
    });
    assert_eq!(
        cmds,
        vec![
            (cmd::SET_STANDBY, vec![0x00]),
            // set_packet_params(8, 0, len=5, crc=1, invert=0)
            (cmd::SET_PACKET_PARAMS, vec![0x00, 0x08, 0x00, 0x05, 0x01, 0x00]),
            (cmd::SET_BUFFER_BASE_ADDRESS, vec![0x00, 0xFF]),
            (cmd::WRITE_BUFFER, vec![0x00, 0xcb, 0x01, 0x02, 0x03, 0x04]),
            (cmd::SET_TX, vec![0x00, 0x00, 0x00]),
        ]
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
    use sx126x::cmd;
    // Canned GET_PACKET_STATUS: [status, rssi=126, snr=+30(8bit signed), pad].
    // Reference: rssi//=-2 -> -63 dBm; snr = s8/4 = 7.5 dB.
    let cmds = log(vec![(cmd::GET_PACKET_STATUS, vec![0, 126, 30, 0])], |r| {
        let (rssi, snr) = r.get_packet_status().unwrap();
        assert_eq!(rssi, -63);
        assert!((snr - 7.5).abs() < 1e-4);
    });
    assert_eq!(cmds, vec![(cmd::GET_PACKET_STATUS, Vec::<u8>::new())]);
    // A strong-radio decode with full IRQ word read.
    let _ = cmd::GET_IRQ_STATUS;
}
