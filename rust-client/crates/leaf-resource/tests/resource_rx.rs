//! Resource RX on the leaf: full receive + hash-verify + completion, and
//! resume across a dormant link window (T4 sustain).

mod common;

use leaf_resource::store::VecBackedStore;
use leaf_resource::{ResourceRx, ResourceTx, Store, RESOURCE_SDU};
use rns_core::resource::ResourceAction;
use rns_core::resource::ResourceStatus;
use rns_crypto::FixedRng;

use common::{decrypt, encrypt, pump_until_complete, test_payload};

#[test]
fn leaf_receives_hashes_and_completes_resource() {
    let payload = test_payload(2000); // ~5 parts at 464-SDU
    let now = 1000.0;
    let link_rtt = 0.1;

    // Gateway advertises a resource to the leaf.
    let mut rng = FixedRng::new(b"t6-rx-gateway-rng");
    let mut tx = ResourceTx::new(&payload, None, RESOURCE_SDU, &encrypt, &mut rng, now, link_rtt)
        .expect("sender");
    let adv = match &tx.advertise(now)[0] {
        ResourceAction::SendAdvertisement(a) => a.clone(),
        _ => panic!("expected advertisement"),
    };

    // Leaf builds a receiver from the advertisement and drives the transfer.
    let mut rx = ResourceRx::from_advertisement(&adv, RESOURCE_SDU, link_rtt, now)
        .expect("receiver from adv");
    pump_until_complete(&mut tx, &mut rx, now);

    let (got, total) = rx.progress();
    assert!(total > 1, "expected a multipart resource, got {total} part(s)");
    assert_eq!(got, total);

    // Reassemble: decrypt, hash-verify against the advertisement, stream to the
    // store, and yield the completion proof to ACK back.
    let mut store = VecBackedStore::new();
    let proof = rx.assemble_with(&decrypt, &mut store).expect("assemble+verify");
    assert!(!proof.is_empty(), "completion proof must be produced");
    assert_eq!(store.take(), payload, "reassembled payload must match the source");
    assert_eq!(rx.status(), ResourceStatus::Complete);
}

#[test]
fn leaf_resumes_across_dormant_gap() {
    // A large-ish multi-window resource; the leaf goes dormant mid-transfer
    // (a link window closes), then resumes from retained receiver state when
    // the next window opens and completes — the T4 sustain-across-gap rule at
    // the Resource layer.
    let payload = test_payload(4000); // ~9 parts → several windows
    let now = 1000.0;
    let link_rtt = 0.1;

    let mut rng = FixedRng::new(b"t6-resume-gateway-rng");
    let mut tx = ResourceTx::new(&payload, None, RESOURCE_SDU, &encrypt, &mut rng, now, link_rtt)
        .expect("sender");
    let adv = match &tx.advertise(now)[0] {
        ResourceAction::SendAdvertisement(a) => a.clone(),
        _ => panic!("no adv"),
    };
    let mut rx = ResourceRx::from_advertisement(&adv, RESOURCE_SDU, link_rtt, now).unwrap();

    // Phase 1: accept + feed parts only until the leaf is genuinely *partial*
    // (some, but not all, parts) — the cut for a dormant gap.
    let mut rx_actions = rx.accept(now);
    let mut made_progress = false;
    for _ in 0..20 {
        let s_actions: Vec<_> = rx_actions
            .iter()
            .flat_map(|a| match a {
                ResourceAction::SendRequest(req) => tx.handle_request(req, now),
                _ => Vec::new(),
            })
            .collect();
        rx_actions = Vec::new();
        for a in s_actions {
            if let ResourceAction::SendPart(p) = a {
                rx_actions.extend(rx.feed_part(&p, now));
            }
        }
        let (p, t) = rx.progress();
        if p > 0 && p < t {
            made_progress = true; // genuinely partial — a good place to sleep
            break;
        }
        if t > 0 && p >= t {
            break; // transfer already finished feeding; not resumable-able here
        }
    }
    assert!(made_progress, "expected a partial transfer to resume, got {:?}", rx.progress());
    let (partial, total) = rx.progress();
    assert!(
        partial > 0 && partial < total,
        "expected partial<total, got {partial}/{total}"
    );

    // Dormant gap: the link window closes; time advances, no parts flow. The
    // receiver (same object) retains parts + window state across the gap.
    let gap_now = now + 60.0;

    // Phase 2: window reopens — finish the transfer with the same receiver.
    pump_until_complete(&mut tx, &mut rx, gap_now);

    let mut store = VecBackedStore::new();
    let proof = rx.assemble_with(&decrypt, &mut store).expect("assemble+verify");
    assert!(!proof.is_empty());
    assert_eq!(store.take(), payload, "resumed transfer must yield the full payload");
    assert_eq!(rx.status(), ResourceStatus::Complete);
}
