//! T7 host E2E — the composed leaf pipeline (design §5 data-flow, minus radio).
//!
//! Runs the whole leaf on the host in order, proving the ticket crates compose:
//!   1. Identity (lma-identity): mint leaf + server; compute the server's
//!      `lxmf/delivery` DEST.
//!   2. Control path (lma-wire → lma-lxmf): build a `SensorReport` envelope,
//!      pack + sign it into an LXMF message addressed to the delivery DEST,
//!      unpack + verify, decode back to the same report.
//!   3. Resource leg (leaf-rns paces leaf-resource): the gateway pushes an RNS
//!      Resource to the leaf, but a part is only applied on the leaf when its
//!      link window is open (`LinkWindowScheduler`) — across several dormant
//!      gaps the full payload is hash-verified and reassembled.
//!   4. PASS or FAIL (exit code).
//!
//! The RF leg (radio-interface as `rns_core::Interface` over the real RNode)
//! is the on-device hardware half of T7.

use std::process::ExitCode;

use lma_identity::{delivery_hash, mint};
use lma_lxmf::{pack_envelope, unpack_envelope_verified};
use lma_wire::{lmao_envelope, reading, sensor_envelope};
use leaf_resource::store::VecBackedStore;
use leaf_resource::{ResourceRx, ResourceTx, Store, RESOURCE_SDU};
use leaf_rns::{LinkWindowConfig, LinkWindowScheduler};
use rns_core::resource::ResourceAction;
use rns_crypto::FixedRng;

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{:02x}", x)).collect()
}

fn fixed_identity(seed: &[u8]) -> rns_crypto::identity::Identity {
    let mut rng = FixedRng::new(seed);
    mint(&mut rng)
}

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

/// Step 1 + 2: identity + LXMF control path (SensorReport to delivery DEST).
fn control_path() {
    let leaf = fixed_identity(b"t7-leaf-key-seed-0001");
    let server = fixed_identity(b"t7-server-key-seed-0001");
    let dest = delivery_hash(&server); // server's lxmf/delivery DEST

    let envelope = sensor_envelope(
        "leaf-e2e",
        1,
        3.3,
        vec![
            reading(1, 42.5, "C", 1700000000123), // die temp
            reading(3, 24.1, "C", 1700000000456), // ambient temp
            reading(2, 55.0, "%", 1700000000789), // humidity
        ],
    );

    let (packed, _msg_hash) = pack_envelope(&leaf, &dest, 1700000000.0, &envelope)
        .expect("lxmf pack+sign");
    assert!(packed.len() > 16 + 16 + 64);

    let (decoded, res) = unpack_envelope_verified(&packed, &leaf).expect("lxmf unpack+verify");
    assert_eq!(res.destination_hash, dest, "addressed to the server's delivery DEST");
    assert_eq!(res.source_hash, *leaf.hash());
    assert_eq!(res.signature_valid, Some(true));

    let report = match decoded.payload.as_ref() {
        Some(lmao_envelope::Payload::Sensor(r)) => r,
        _ => panic!("expected Sensor variant"),
    };
    assert_eq!(report.node_id, "leaf-e2e");
    assert_eq!(report.readings.len(), 3);

    let mut rebuf = Vec::new();
    use prost::Message;
    decoded.encode(&mut rebuf).unwrap();
    println!(
        "  control: SensorReport(LXMF) -> delivery {} verified; {} readings; wire {} B",
        hex(&dest),
        report.readings.len(),
        rebuf.len()
    );
}

/// Step 3: a gateway Resource to the leaf, paced by the leaf's link windows.
fn resource_paced_by_link_window() {
    let payload = test_payload(5000); // ~11 parts at 464-SDU → several windows
    let now = 1000.0;
    let link_rtt = 0.1;

    // Gateway advertises.
    let mut rng = FixedRng::new(b"t7-gateway-rng");
    let mut tx = ResourceTx::new(&payload, None, RESOURCE_SDU, &encrypt, &mut rng, now, link_rtt)
        .expect("gateway sender");
    let adv = match &tx.advertise(now)[0] {
        ResourceAction::SendAdvertisement(a) => a.clone(),
        _ => panic!("no adv"),
    };

    // Leaf receiver over a held, duty-cycled link.
    let mut rx = ResourceRx::from_advertisement(&adv, RESOURCE_SDU, link_rtt, now)
        .expect("leaf receiver");
    let mut sched = LinkWindowScheduler::new(LinkWindowConfig::new(160, 840));
    let mut t_ms: u32 = 0;
    sched.mark_link_established(t_ms);

    let mut rx_actions = rx.accept(now);
    let mut windows_crossed = 0u32;
    let mut guard = 0;
    while !rx.is_complete() && guard < 2_000_000 {
        guard += 1;

        // Gateway serves the leaf's outstanding part requests.
        let mut outbound_parts: Vec<Vec<u8>> = Vec::new();
        for a in rx_actions {
            match a {
                ResourceAction::SendRequest(req) => {
                    for sa in tx.handle_request(&req, now) {
                        if let ResourceAction::SendPart(p) = sa {
                            outbound_parts.push(p);
                        }
                    }
                }
                ResourceAction::Failed(e) => panic!("leaf failed: {e:?}"),
                _ => {}
            }
        }
        rx_actions = Vec::new();

        // Each part is delivered to the leaf only inside an open link window;
        // the leaf sleeps (dormant gap) in between — the T4 sustain rule.
        for p in outbound_parts {
            let mut waited = false;
            while !sched.is_rx_awake(t_ms) {
                t_ms += 50; // advance across a dormant gap toward the next window
                waited = true;
            }
            if waited {
                windows_crossed += 1;
            }
            rx_actions.extend(rx.feed_part(&p, now));
            sched.note_link_activity(t_ms);
            t_ms += 20;
        }
        // Bound the schedule clock so very long transfers don't stall the test.
        if t_ms > 1_000_000 {
            // keep the window rendezvous alive by wrapping within the schedule
            t_ms %= 1_400_000;
        }
    }

    assert!(rx.is_complete(), "resource never completed across windows");
    let mut store = VecBackedStore::new();
    let proof = rx.assemble_with(&decrypt, &mut store).expect("assemble+verify");
    assert!(!proof.is_empty());
    assert_eq!(store.take(), payload, "leaf reassembled the gateway's resource");
    println!(
        "  resource: RNS Resource reassembled+hash-verified by the leaf across {} link windows; {} B; proof {} B",
        windows_crossed + 1,
        payload.len(),
        proof.len()
    );
}

fn main() -> ExitCode {
    println!("[t7-e2e] composed leaf pipeline (design §5 data-flow, host)");
    println!("step 1/2 — identity + LXMF SensorReport control path:");
    control_path();
    println!("step 3 — RNS Resource over the held link, paced by link windows:");
    resource_paced_by_link_window();
    println!("[t7-e2e] PASS — leaf control + resource pipeline composes on the host");
    ExitCode::SUCCESS
}
