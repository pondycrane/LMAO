//! The leaf radio `Interface` contract (the µReticulum transcription): reference
//! metadata defaults, inbound processing (stats + rssi/snr stamping onto the
//! rns-core packet), and a raw-packet round-trip through rns-core's decode.

use radio_interface::interface::{InterfaceMode, RadioInterface};
use rns_core::packet::{PacketFlags, RawPacket};

fn plain_flags() -> PacketFlags {
    PacketFlags { header_type: 0, context_flag: 0, transport_type: 0, destination_type: 0, packet_type: 0 }
}

fn sample_radio() -> RadioInterface {
    let mut r = RadioInterface::new("cardputer_sx1262");
    r.online = true;
    r.bitrate = 125_000;
    r.rssi = Some(-63); // a plausible LoRa RSSI (dBm)
    r.snr = Some(7.5);
    r
}

#[test]
fn reference_defaults_match_microreticulum() {
    let r = RadioInterface::default();
    assert!(!r.online);
    assert!(r.enabled);
    assert_eq!(r.mode, InterfaceMode::Full);
    assert_eq!(r.bitrate, 0);
    assert_eq!(r.mtu, 500);
    assert_eq!(r.hw_mtu, 500);
    assert!(r.out && r.inn);
    assert_eq!(r.rssi, None);
    assert_eq!(r.snr, None);
    assert_eq!(r.stats.rx_frames, 0);
}

#[test]
fn process_incoming_stamps_interface_signal_on_packet() {
    // Build a real HEADER_1 packet frame via rns-core (the protocol itself).
    let mut dest = [0u8; 16];
    dest[..4].copy_from_slice(&[0xde, 0xad, 0xbe, 0xef]);
    let data = b"sensor heartbeat payload";
    let pkt = RawPacket::pack(
        plain_flags(),
        0,
        &dest,
        None,
        0, // CONTEXT_NONE
        data,
    )
    .unwrap();

    let mut iface = sample_radio();
    let now = 12_345;
    let incoming = iface.process_incoming(&pkt.raw, now).expect("decodes");
    assert_eq!(incoming.rssi, Some(-63), "packet.rssi = interface.rssi");
    assert_eq!(incoming.snr, Some(7.5));
    assert_eq!(incoming.destination_hash, dest);
    assert_eq!(incoming.data, data);
    assert_eq!(iface.stats.rx_frames, 1);
    assert_eq!(iface.stats.rx_bytes, pkt.raw.len() as u64);
    assert_eq!(iface.stats.last_activity_ms, now);
}

#[test]
fn raw_packet_roundtrip_pack_then_inbound_decode() {
    let mut dest = [0u8; 16];
    dest.copy_from_slice(&[
        1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16,
    ]);
    let mut tx_id = [0u8; 16];
    tx_id[15] = 0xab;

    // HEADER_2 carries transport_id (HEADER_1 would drop it on unpack).
    let hdr2 = PacketFlags { header_type: 1, context_flag: 0, transport_type: 0, destination_type: 0, packet_type: 0 };
    let pkt = RawPacket::pack(
        hdr2,
        3,
        &dest,
        Some(&tx_id),
        1, // CONTEXT_RESOURCE
        b"adv-secondary",
    )
    .unwrap();

    let mut iface = sample_radio();
    let back = iface.process_incoming(&pkt.raw, 0).unwrap();
    assert_eq!(back.hops, 3);
    assert_eq!(back.destination_hash, dest);
    assert_eq!(back.transport_id, Some(tx_id));
    assert_eq!(back.context, 1);
    assert_eq!(back.data, b"adv-secondary");
    assert_eq!(back.packet_hash, pkt.packet_hash, "hash must survive the radio");
}

#[test]
fn header1_fits_the_464_byte_opp_budget() {
    // The opportunistic budget for control frames (design §6b: 240 B data +
    // framing); here the whole HEADER_1 packet must sit under the LoRa SDU.
    let mut dest = [0u8; 16];
    dest[0] = 0xff;
    let payload = vec![0x55u8; 240];
    let pkt = RawPacket::pack(plain_flags(), 0, &dest, None, 0, &payload).unwrap();
    assert!(pkt.raw.len() <= 464, "raw {} B must fit the 464-B LoRa frame", pkt.raw.len());

    let mut iface = sample_radio();
    let back = iface.process_incoming(&pkt.raw, 0).unwrap();
    assert_eq!(back.data.len(), 240);
}

#[test]
fn outgoing_tally_and_close() {
    let mut iface = sample_radio();
    iface.note_outgoing(&[0xcb, 0x01, 0x02]);
    iface.note_outgoing(&[0x50, 0x03]);
    assert_eq!(iface.stats.tx_frames, 2);
    assert_eq!(iface.stats.tx_bytes, 5);

    iface.close();
    assert!(!iface.online && !iface.enabled);
}
