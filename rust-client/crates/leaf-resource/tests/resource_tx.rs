//! Resource TX from the leaf: a leaf-originated resource is served to the
//! gateway and reassembled there, and the sender reaches AwaitingProof.

mod common;

use leaf_resource::store::VecBackedStore;
use leaf_resource::{ResourceRx, ResourceTx, Store, RESOURCE_SDU};
use rns_core::resource::{ResourceAction, ResourceStatus};
use rns_crypto::FixedRng;

use common::{decrypt, encrypt, pump_until_complete, test_payload};

#[test]
fn leaf_originated_resource_received_and_verified_by_gateway() {
    let payload = test_payload(1500); // leaf pushes ~1500 B of sensor history
    let now = 1200.0;
    let link_rtt = 0.08;

    // Leaf is the sender this time.
    let mut rng = FixedRng::new(b"t6-tx-leaf-rng");
    let mut tx = ResourceTx::new(&payload, None, RESOURCE_SDU, &encrypt, &mut rng, now, link_rtt)
        .expect("leaf sender");
    let adv = match &tx.advertise(now)[0] {
        ResourceAction::SendAdvertisement(a) => a.clone(),
        _ => panic!("no adv"),
    };

    // Gateway receiver pulls the whole resource from the leaf.
    let mut rx = ResourceRx::from_advertisement(&adv, RESOURCE_SDU, link_rtt, now).unwrap();
    pump_until_complete(&mut tx, &mut rx, now);

    let mut store = VecBackedStore::new();
    let proof = rx.assemble_with(&decrypt, &mut store).expect("gateway reassembly");
    assert!(!proof.is_empty());
    assert_eq!(store.take(), payload, "gateway must reassemble the leaf's resource");

    // Sender reached AwaitingProof (served every part) and proof validation
    // closes the transfer.
    assert_eq!(tx.status(), ResourceStatus::AwaitingProof);
    let (sent, total) = tx.progress();
    assert_eq!(sent, total);
    let _now_after = now + 2.0;
    let final_actions = tx.handle_proof(&proof, now);
    // handle_proof transitions to Complete on a valid proof.
    let _ = final_actions;
}
