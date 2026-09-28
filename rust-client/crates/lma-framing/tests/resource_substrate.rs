//! T8 "on the Resource substrate": the successor framing rides ON the stable
//! RNS Resource layer (T6/T7). A gateway pushes the attachment payload as an
//! RNS Resource; the leaf reassembles it over the link, then the framing core
//! splits it back into the manifest's chunks, re-checks each CRC, and
//! whole-payload SHA-256 — the receiver-side of the §6c framing on Resource.

use lma_framing::{FeedResult, Reassembler, crc32};
use lma_wire::{LmafKind, LmafManifest};
use leaf_resource::store::VecBackedStore;
use leaf_resource::{ResourceRx, ResourceTx, Store, RESOURCE_SDU};
use rns_core::resource::ResourceAction;
use rns_crypto::sha256::sha256;
use rns_crypto::FixedRng;

const CHUNK_SIZE: u32 = 32;

fn xor(key: u8, d: &[u8]) -> Vec<u8> {
    d.iter().map(|b| b ^ key).collect()
}
fn encrypt(d: &[u8]) -> Vec<u8> {
    xor(0x5a, d)
}
fn decrypt(d: &[u8]) -> Result<Vec<u8>, ()> {
    Ok(xor(0x5a, d))
}

fn test_payload(bytes: usize) -> Vec<u8> {
    (0..bytes as u16).map(|i| (i.wrapping_mul(29) / 3 % 251) as u8).collect()
}

/// Drive gateway(tx)->leaf(rx) resource until every part is on the leaf.
fn resource_to_leaf(tx: &mut ResourceTx, rx: &mut ResourceRx, now: f64) {
    let mut rx_actions = rx.accept(now);
    let mut guard = 0;
    while !rx.is_complete() && guard < 5000 {
        guard += 1;
        let s_actions: Vec<_> = rx_actions
            .iter()
            .flat_map(|a| match a {
                ResourceAction::SendRequest(req) => tx.handle_request(req, now),
                _ => Vec::new(),
            })
            .collect();
        rx_actions = Vec::new();
        for a in s_actions {
            match a {
                ResourceAction::SendPart(p) => rx_actions.extend(rx.feed_part(&p, now)),
                ResourceAction::SendHmu(h) => rx_actions.extend(rx.feed_hmu(&h, now)),
                _ => {}
            }
        }
    }
    assert!(rx.is_complete(), "resource to leaf stalled at {:?}", rx.progress());
}

#[test]
fn framed_attachment_delivered_by_resource_and_verified() {
    let payload = test_payload(2000);
    let digest = sha256(&payload);
    let count = payload.len().div_ceil(CHUNK_SIZE as usize) as u32;

    let manifest = LmafManifest {
        id: digest[..16].to_vec(),
        payload_sha256: digest.to_vec(),
        kind: LmafKind::LmafChart as i32,
        codec: "lmao:chart-line-v1".to_owned(),
        chunk_size: CHUNK_SIZE,
        chunk_count: count,
        total_bytes: payload.len() as u64,
        sample_rate: 0,
        channels: 0,
        duration_ms: 0,
        width: 640,
        height: 400,
        node_id: "leaf-e2e".to_owned(),
        created_ms: 1700000000456,
    };

    // The gateway ships the attachment as an RNS Resource over the link.
    let now = 1000.0;
    let mut rng = FixedRng::new(b"t8-resource-gateway");
    let mut tx = ResourceTx::new(&payload, None, RESOURCE_SDU, &encrypt, &mut rng, now, 0.1)
        .expect("gateway sender");
    let adv = match &tx.advertise(now)[0] {
        ResourceAction::SendAdvertisement(a) => a.clone(),
        _ => panic!("no adv"),
    };
    let mut rx = ResourceRx::from_advertisement(&adv, RESOURCE_SDU, 0.1, now)
        .expect("leaf receiver");
    resource_to_leaf(&mut tx, &mut rx, now);

    // Leaf reassembles the raw attachment from the Resource.
    let mut store = VecBackedStore::new();
    rx.assemble_with(&decrypt, &mut store).expect("assemble+verify");
    let raw = store.take();
    assert_eq!(raw, payload, "Resource delivered the attachment intact");

    // The framing layer splits it back into the manifest's chunks and verifies.
    let mut reasm = Reassembler::from_manifest(&manifest);
    for i in 0..count {
        let start = (i as usize) * CHUNK_SIZE as usize;
        let end = core::cmp::min(start + CHUNK_SIZE as usize, raw.len());
        let chunk = raw[start..end].to_vec();
        let framed = lma_wire::LmafChunk {
            id: manifest.id.clone(),
            index: i,
            data: chunk,
            crc: crc32(&raw[start..end]),
        };
        assert_eq!(reasm.feed_chunk(&framed), FeedResult::Accepted, "chunk {i}");
    }

    let (assembled, verified) = reasm.reassemble();
    assert!(verified, "whole-payload SHA-256 must match the manifest digest");
    assert_eq!(assembled, payload);

    let ack = reasm.build_ack();
    assert_eq!(ack.status, lma_wire::LmafAckStatus::LmafComplete as i32);
    assert!(ack.missing.is_empty());
}
