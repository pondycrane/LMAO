//! Shared helpers for the leaf-resource host tests.
#![allow(dead_code)]

use leaf_resource::{ResourceRx, ResourceTx};
use rns_core::resource::ResourceAction;

/// Fixed reversible link-cipher stand-in for the host tests. The real leaf
/// uses the RNS link AES-256-CBC; the Resource protocol treats the cipher
/// opaquely, so any reciprocal pair drives the machinery (hash-verify is over
/// the decrypted bytes).
pub fn xor(key: u8, d: &[u8]) -> Vec<u8> {
    d.iter().map(|b| b ^ key).collect()
}
pub fn encrypt(d: &[u8]) -> Vec<u8> {
    xor(0x5a, d)
}
pub fn decrypt(d: &[u8]) -> Result<Vec<u8>, ()> {
    Ok(xor(0x5a, d))
}

/// A deterministic, lightly-structured payload (leniently over one resource SDU
/// so multipart/window/hashmap machinery is exercised).
pub fn test_payload(bytes: usize) -> Vec<u8> {
    (0..bytes as u16).map(|i| (i.wrapping_mul(29) / 3 % 251) as u8).collect()
}

/// Drive the gateway (`tx`) / leaf (`rx`) pair until the leaf has every part.
pub fn pump_until_complete(tx: &mut ResourceTx, rx: &mut ResourceRx, now: f64) {
    let mut rx_actions = rx.accept(now);
    let mut guard = 0;
    while !rx.is_complete() && guard < 2000 {
        guard += 1;
        let mut s_actions = Vec::new();
        for a in rx_actions {
            match a {
                ResourceAction::SendRequest(req) => {
                    s_actions.extend(tx.handle_request(&req, now))
                }
                ResourceAction::Failed(e) => panic!("leaf failed: {e:?}"),
                ResourceAction::SendCancelReceiver(_) => panic!("gateway rejected"),
                _ => {}
            }
        }
        rx_actions = Vec::new();
        for a in s_actions {
            match a {
                ResourceAction::SendPart(p) => rx_actions.extend(rx.feed_part(&p, now)),
                ResourceAction::SendHmu(h) => rx_actions.extend(rx.feed_hmu(&h, now)),
                _ => {}
            }
        }
    }
    assert!(
        rx.is_complete(),
        "transfer never completed; progress={:?}, status={:?}",
        rx.progress(),
        rx.status()
    );
}
