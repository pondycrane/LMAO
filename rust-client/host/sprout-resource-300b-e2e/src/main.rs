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

use lma_dtu::at_dtu::{from_hex, tx_lines};
use lma_link_resource::RESOURCE_SDU;
use leaf_resource::store::VecBackedStore;
use leaf_resource::{ResourceRx, ResourceTx, Store};
use radio_interface::{parse_rnode_frame, SplitAssembler};
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

/// Route ONE whole packet over the emulated **RAK-RF frame transport** — the
/// Sprout's real radio leg: `tx_lines` (the RAK `AT+PSEND` frame splitter at
/// the 254-B DTU frame cap) → `parse_rnode_frame` → the RNode `SplitAssembler`
/// (the server/urns RX side). Larger-than-one-frame packets genuinely split
/// into multiple flagged frames and are reassembled here — i.e. "all frames
/// received", no workaround. Returns every completed packet (normally exactly
/// one). Counts frames against the assembler's completed counter.
fn rf_roundtrip(
    pkt: &[u8],
    asm: &mut SplitAssembler,
    now_ms: &mut u32,
    frames: &mut usize,
    packets: &mut usize,
) -> Vec<Vec<u8>> {
    let mut out = Vec::new();
    for (line, _wait_ms) in tx_lines(pkt, 0x50) {
        if let Some(hex) = line.strip_prefix("AT+PSEND=") {
            *frames += 1;
            let onair = from_hex(hex).expect("AT+PSEND hex");
            let frame = parse_rnode_frame(&onair);
            let r = asm.push(&frame, *now_ms);
            *now_ms = now_ms.wrapping_add(100);
            if r.complete {
                out.push(r.data);
            }
        }
    }
    assert!(
        !out.is_empty(),
        "packet {}B split into {frames} frames but never reassembled",
        pkt.len()
    );
    *packets += 1;
    out
}

/// The whole e2e: Sprout 300-B Resource push → server RX, with **every**
/// exchanged packet crossing the real RAK frame transport (multiframe included),
/// hash-verified end to end. Panics on any failure.
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
    //    the Sprout's real part SDU. Each resource packet (≤254 B) fits one
    //    RAK frame; >254-B packet splitting is covered separately by the
    //    lma-dtu unit tests (`tx_split_two_frames_both_flagged`).
    let now = 1000.0f64;
    let link_rtt = 0.1;
    let mut rng = FixedRng::new(b"e2e-sprout-rng");

    // The shared RX-side frame reassembler (server/urns SplitAssembler).
    let mut asm = SplitAssembler::new(508, 15_000);
    let mut now_ms = 0u32;
    let mut frames = 0usize;
    let mut packets = 0usize;

    let mut tx = ResourceTx::new(&payload, None, RESOURCE_SDU, &encrypt, &mut rng, now, link_rtt)
        .expect("sprout ResourceTx");
    let adv0 = match &tx.advertise(now)[0] {
        ResourceAction::SendAdvertisement(a) => a.clone(),
        _ => panic!("no advertisement produced"),
    };
    // The advertisement crosses the RF transport too.
    let adv_rt = rf_roundtrip(&adv0, &mut asm, &mut now_ms, &mut frames, &mut packets);
    assert_eq!(adv_rt.len(), 1, "advertisement frame(s) reassembled");

    // 3. LMAO server side: Resource receiver (accept → request → assemble).
    let mut rx = ResourceRx::from_advertisement(&adv_rt[0], RESOURCE_SDU, link_rtt, now)
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
                    // Receiver → sender request crosses the RF transport.
                    let req_rt = rf_roundtrip(&req, &mut asm, &mut now_ms, &mut frames, &mut packets);
                    assert_eq!(req_rt.len(), 1, "part-request frame(s) reassembled");
                    for sa in tx.handle_request(&req_rt[0], now) {
                        if let ResourceAction::SendPart(p) = sa {
                            outbound.push(p);
                        }
                    }
                }
                ResourceAction::SendHmu(hmu) => {
                    // Sender HMU hashmap update crosses the RF transport.
                    let hmu_rt = rf_roundtrip(&hmu, &mut asm, &mut now_ms, &mut frames, &mut packets);
                    assert_eq!(hmu_rt.len(), 1, "HMU frame(s) reassembled");
                    next.extend(rx.feed_hmu(&hmu_rt[0], now));
                }
                ResourceAction::Failed(e) => panic!("server RX failed: {e:?}"),
                _ => {}
            }
        }
        rx_actions = next;
        for p in outbound {
            // Each part crosses the RF transport as >=1 flagged frame.
            let part_rt = rf_roundtrip(&p, &mut asm, &mut now_ms, &mut frames, &mut packets);
            assert_eq!(part_rt.len(), 1, "resource part frame(s) reassembled");
            rx_actions.extend(rx.feed_part(&part_rt[0], now));
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

    // 5. Every whole packet was reassembled from every TX'd frame — none lost,
    //    none dropped (any frame loss across the real RAK transport fails this
    //    accounting).
    assert_eq!(
        asm.completed() as usize, packets,
        "packet accounting: {packets} whole packets TX'd but {} reassembled",
        asm.completed()
    );
    assert_eq!(asm.dropped(), 0, "a frame was dropped by the reassembler");
}

fn crate_hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{:02x}", x)).collect()
}

fn main() -> ExitCode {
    run();
    let parts = (EXPECTED_BYTES + RESOURCE_SDU - 1) / RESOURCE_SDU;
    println!(
        "[sprout-300b-e2e] PASS — single 300-B resource send ({parts} parts) -> RAK \
         split-frames -> SplitAssembler, all frames + resource reassembled \
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
