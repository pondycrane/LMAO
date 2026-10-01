//! E2E: Cardputer 500-byte resource → LMAO server (host, deterministic).
//!
//! The Cardputer firmware (`rust-client/firmware/src/main.rs`) pushes
//! `(0..500u16).map(|i| (i % 251) as u8)` as an RNS Resource over the held
//! link; the k8s Rust receiver (`lmao-server-rust-recv` on tp4, Heltec RNode)
//! reassembled it live over LoRa:
//!
//! ```text
//! RESOURCE received link=LinkId(08821942…c03) bytes=500
//!           sha256=f6b8396506ad2ac31bfe6d73fa0155e090b62b4321043dafe308090296b28d84
//! ```
//!
//! This test re-runs the *same payload* through the leaf `ResourceTx`
//! (cardputer side) → server `ResourceRx` (LMAO side) state machines on the
//! host and asserts the reassembled 500 bytes hash to that exact, LoRa-proven
//! sha256. It is a deterministic, hardware-free cross-check of the real RF
//! path and pins the resource wire-format to the deployed receiver.

use std::process::ExitCode;

use leaf_resource::store::VecBackedStore;
use leaf_resource::{ResourceRx, ResourceTx, Store, RESOURCE_SDU};
use rns_core::resource::ResourceAction;
use rns_crypto::sha256::sha256;
use rns_crypto::FixedRng;

const EXPECTED_BYTES: usize = 500;

/// The whole e2e: cardputer 500-B Resource push → LMAO server RX + hardware
/// sha256 cross-check. Panics on any failure (also detaches the runnable logic
/// from `main` so `rust_test` can execute it directly under Bazel).
fn run() {
    // 1. Pin the payload to the hardware-proven vector before touching the
    //    transfer: if the firmware payload pattern ever changes, this fails
    //    loudly instead of silently testing a different set of bytes.
    let payload = cardputer_payload();
    assert_eq!(payload.len(), EXPECTED_BYTES);
    let payload_sha = sha256(&payload);
    assert_eq!(
        payload_sha, HW_SHA256,
        "firmware payload sha256 changed; update this test to match the new RF target"
    );

    // 2. Cardputer side: Resource sender (advertise → serve parts → proof).
    let now = 1000.0f64;
    let link_rtt = 0.1;
    let mut rng = FixedRng::new(b"e2e-cardputer-rng");
    let mut tx = ResourceTx::new(&payload, None, RESOURCE_SDU, &encrypt, &mut rng, now, link_rtt)
        .expect("cardputer ResourceTx");
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

    // 4. Assemble hash-verified + cross-check against the LoRa-proven digest.
    let mut store = VecBackedStore::new();
    let proof = rx
        .assemble_with(&decrypt, &mut store)
        .expect("assemble + hash-verify failed");
    assert!(!proof.is_empty(), "expected a completion proof");

    let data = store.take();
    assert_eq!(data.len(), EXPECTED_BYTES, "server reassembled wrong byte count");
    assert_eq!(data, payload, "server reassembled payload differs from cardputer");
    assert_eq!(sha256(&data), HW_SHA256, "reassembled sha256 != LoRa-proven value");
}

/// The exact payload the Cardputer firmware pushes (main.rs).
fn cardputer_payload() -> Vec<u8> {
    (0..500u16).map(|i| (i % 251) as u8).collect()
}

/// sha256 of the firmware's 500-B `(i % 251)` payload, as captured live on the
/// k8s receiver over real LoRa (2026-10-01).
const HW_SHA256: [u8; 32] = [
    0xf6, 0xb8, 0x39, 0x65, 0x06, 0xad, 0x2a, 0xc3, 0x1b, 0xfe, 0x6d, 0x73, 0xfa, 0x01, 0x55, 0xe0,
    0x90, 0xb6, 0x2b, 0x43, 0x21, 0x04, 0x3d, 0xaf, 0xe3, 0x08, 0x09, 0x02, 0x96, 0xb2, 0x8d, 0x84,
];

fn encrypt(d: &[u8]) -> Vec<u8> {
    d.iter().map(|b| b ^ 0x5a).collect()
}

fn decrypt(d: &[u8]) -> Result<Vec<u8>, ()> {
    Ok(d.iter().map(|b| b ^ 0x5a).collect())
}

fn main() -> ExitCode {
    run();
    let parts = (cardputer_payload().len() + RESOURCE_SDU - 1) / RESOURCE_SDU;
    println!(
        "[500b-e2e] PASS — cardputer 500-B resource ({parts} parts) reassembled \
         hash-verified; sha256={}",
        crate_hex(&HW_SHA256)
    );
    ExitCode::SUCCESS
}

fn crate_hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{:02x}", x)).collect()
}

#[cfg(test)]
mod tests {
    use super::run;

    #[test]
    fn cardputer_500b_resource_reaches_server() {
        run();
    }
}
