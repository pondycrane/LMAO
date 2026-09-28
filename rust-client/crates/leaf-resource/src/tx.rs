//! Resource TX on the leaf — wraps `rns_core::resource::ResourceSender`.

use alloc::vec::Vec;

use rns_core::resource::{ResourceAction, ResourceError, ResourceSender, ResourceStatus};
use rns_core::buffer::types::NoopCompressor;
use rns_crypto::Rng;

/// Leaf-side sender of an RNS Resource over a held Link.
///
/// The payload is encrypted with the link cipher (`encrypt_fn`), split into
/// hash-mapped parts, and served to the gateway's part requests. A leaf uses
/// this to push sensor history / firmware-style payloads to the gateway.
pub struct ResourceTx {
    inner: ResourceSender,
    total_parts: usize,
}

impl ResourceTx {
    /// Create a sender for `data`. `encrypt_fn` is the link-layer cipher.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        data: &[u8],
        metadata: Option<&[u8]>,
        sdu: usize,
        encrypt_fn: &dyn Fn(&[u8]) -> Vec<u8>,
        rng: &mut dyn Rng,
        now: f64,
        link_rtt: f64,
    ) -> Result<Self, ResourceError> {
        let inner = ResourceSender::new(
            data,
            metadata,
            sdu,
            encrypt_fn,
            &NoopCompressor,
            rng,
            now,
            // auto_compress: off — a leaf's payload is typically already dense.
            false,
            // is_response / request_id / segments / original_hash: single
            // leaf-initiated segment.
            false,
            None,
            1,
            1,
            None,
            link_rtt,
            0.0,
        )?;
        let total_parts = inner.total_parts();
        Ok(ResourceTx { inner, total_parts })
    }

    /// Advertise the resource to the gateway (emits the advertisement packet).
    pub fn advertise(&mut self, now: f64) -> Vec<ResourceAction> {
        self.inner.advertise(now)
    }

    /// Serve a part request from the gateway (emits parts / hashmap-update).
    pub fn handle_request(&mut self, request: &[u8], now: f64) -> Vec<ResourceAction> {
        self.inner.handle_request(request, now)
    }

    /// Complete the transfer when the gateway returns the completion proof.
    pub fn handle_proof(&mut self, proof: &[u8], now: f64) -> Vec<ResourceAction> {
        self.inner.handle_proof(proof, now)
    }

    /// Fraction of parts served.
    pub fn progress(&self) -> (usize, usize) {
        (self.inner.sent_parts, self.total_parts)
    }

    /// Current status.
    pub fn status(&self) -> ResourceStatus {
        self.inner.status
    }
}
