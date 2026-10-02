//! E2E: Sprout 300-byte resource → LMAO server (host, deterministic).
//!
//! Feature-parity twin of `resource-500b-e2e` (the Cardputer's cross-check):
//! the Sprout firmware (`firmware-sprout`) pushes its SensorReport as an RNS
//! Resource through the shared `lma-link-resource` driver — the same
//! `leaf_resource::ResourceTx` (part SDU `lma_link_resource::RESOURCE_SDU`) the
//! Cardputer drives — so the WIRE FORMAT (advertise → request → parts → proof)
//! is identical to the Cardputer's. The payload here is the Sprout's 300-B RF
//! target (kept under the Cardputer's 500 B because the Atom Lite has a smaller
//! heap; 300 B @ SDU 160 = 2 encrypted part carriers, each a single ≤254-B
//! frame).
//!
//! Once a Sprout push is captured on the k8s receiver, swap the pinned hash
//! below for the LoRa-proven digest — it currently pins the deterministic
//! assembly target of `(0..300).map(|i| i % 251)`.

use std::process::ExitCode;

use lma_link_resource::RESOURCE_SDU;
use leaf_resource::store::VecBackedStore;
use leaf_resource::{ResourceRx, ResourceTx, Store};
use rns_core::resource::ResourceAction;
use rns_crypto::sha256::sha256;
use rns_crypto::FixedRng;

const EXPECTED_BYTES: usize = 300;

/// sha256 of `(0..300).map(|i| (i % 251))` — the deterministic host assembly
/// target for the Sprout's 300-B resource. Replace with the LoRa-captured
/// digest once a real push lands on the k8s receiver.
const HW_SHA256: [u8; 32] = [
    0x43, 0xf9, 0xb5, 0xd5, 0x9e, 0xb1, 0x08, 0x81, 0x71, 0x76, 0xc6, 0xf6, 0x5c, 0x2c, 0x62, 0x03,
    0xa2, 0x2f, 0x2a, 0xe8, 0xbc, 0x28, 0xb7, 0xa1, 0xdd, 0xe4, 0x59, 0x47, 0x67, 0x8c, 0x50, 0x42,
];

/// The exact payload the Sprout firmware pushes as a Resource: 300 B, the
/// same `(i % 251)` byte pattern the Cardputer firmware uses (issue #197).
fn sprout_payload() -> Vec<u8> {
    (0..EXPECTED_BYTES as u16).map(|i| (i % 251) as u8).collect()
}

fn encrypt(d: &[u8]) -> Vec<u8> {
    d.iter().map(|b| b ^ 0x5a).collect()
}

fn decrypt(d: &[u8]) -> Result<Vec<u8>, ()> {
    Ok(d.iter().map(|b| b ^ 0x5a).collect())
}

/// The whole e2e: Sprout 300-B Resource push → server RX, hash-verified.
/// Panics on any failure (also detaches the runnable logic from `main` so
/// `rust_test` can execute it directly under Bazel).
fn run() {
    // 1. Pin the payload to the deterministic target before touching the
    //    transfer: if the firmware payload pattern ever changes, this fails
    //    loudly instead of silently testing different bytes.
    let payload = sprout_payload();
    assert_eq!(payload.len(), EXPECTED_BYTES);
    let payload_sha = sha256(&payload);
    assert_eq!(
        payload_sha, HW_SHA256,
        "sprout payload sha256 changed; update this test to match the new RF target"
    );

    // 2. Sprout side: Resource sender (advertise → serve parts → proof) with
    //    the Sprout's real part SDU (160 B ⇒ a single ≤254-B link frame each).
    let now = 1000.0f64;
    let link_rtt = 0.1;
    let mut rng = FixedRng::new(b"e2e-sprout-rng");
    let mut tx = ResourceTx::new(&payload, None, RESOURCE_SDU, &encrypt, &mut rng, now, link_rtt)
        .expect("sprout ResourceTx");
    let adv = match &tx.advertise(now)[0] {
        ResourceAction::SendAdvertisement(a) => a.clone(),
        _ => panic!("no advertisement produced"),
    };

    // 3. LMAO server side: Resource receiver (accept → request → assemble).
    let mut rx = ResourceRx::from_advertisement(&adv, RESOURCE_SDU, link_rtt, now)
        .expect("server ResourceRx");

    let mut rx_actions = rx.accept(now);
    let mut guard = 0u32;
    while !rx.is_complete() {
        guard += 1;
        assert!(guard < 1_000_000, "resource transfer did not converge");

        let mut outbound: Vec<Vec<u8>> = Vec::new();
        let mut next = Vec::new();
        for a in rx_actions {
            match a {
                ResourceAction::SendRequest(req) => {
                    for sa in tx.handle_request(&req, now) {
                        if let ResourceAction::SendPart(p) = sa {
                            outbound.push(p);
                        }
                    }
                }
                ResourceAction::SendHmu(hmu) => {
                    next.extend(rx.feed_hmu(&hmu, now));
                }
                ResourceAction::Failed(e) => panic!("server RX failed: {e:?}"),
                _ => {}
            }
        }
        rx_actions = next;
        for p in outbound {
            rx_actions.extend(rx.feed_part(&p, now));
        }
    }

    // 4. Assemble hash-verified + cross-check against the pinned digest.
    let mut store = VecBackedStore::new();
    let proof = rx
        .assemble_with(&decrypt, &mut store)
        .expect("assemble + hash-verify failed");
    assert!(!proof.is_empty(), "expected a completion proof");

    let data = store.take();
    assert_eq!(data.len(), EXPECTED_BYTES, "server reassembled wrong byte count");
    assert_eq!(data, payload, "server reassembled payload differs from sprout");
    assert_eq!(sha256(&data), HW_SHA256, "reassembled sha256 != pinned value");
}

fn crate_hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{:02x}", x)).collect()
}

fn main() -> ExitCode {
    run();
    let parts = (EXPECTED_BYTES + RESOURCE_SDU - 1) / RESOURCE_SDU;
    println!(
        "[sprout-300b-e2e] PASS — sprout 300-B resource ({parts} parts) reassembled \
         hash-verified; sha256={}",
        crate_hex(&HW_SHA256)
    );
    ExitCode::SUCCESS
}

#[cfg(test)]
mod tests {
    use super::run;

    #[test]
    fn sprout_300b_resource_pushes_to_server() {
        run();
    }
}
