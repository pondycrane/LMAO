//! Resource RX on the leaf — wraps `rns_core::resource::ResourceReceiver`.

use alloc::vec::Vec;

use rns_core::resource::{ResourceAction, ResourceError, ResourceReceiver, ResourceStatus};
use rns_core::buffer::types::{Compressor, NoopCompressor};

use crate::store::Store;

/// Leaf-side receiver of an RNS Resource over a held Link.
///
/// Drives `rns_core`'s `ResourceReceiver` state machine. The raw (encrypted)
/// part bytes live on the heap; `assemble` decrypts (link cipher), hash-verifies
/// against the advertisement, and streams the result into the `Store` plus a
/// completion proof to ACK back.
pub struct ResourceRx {
    inner: ResourceReceiver,
}

impl ResourceRx {
    /// Build a receiver from the gateway's resource advertisement packet.
    pub fn from_advertisement(
        adv: &[u8],
        sdu: usize,
        link_rtt: f64,
        now: f64,
    ) -> Result<Self, ResourceError> {
        Ok(ResourceRx {
            inner: ResourceReceiver::from_advertisement(adv, sdu, link_rtt, now, None, None)?,
        })
    }

    /// Accept and start the transfer (emits the first part request).
    pub fn accept(&mut self, now: f64) -> Vec<ResourceAction> {
        self.inner.accept(now)
    }

    /// Feed a resource part (raw encrypted chunk) from the gateway.
    pub fn feed_part(&mut self, part: &[u8], now: f64) -> Vec<ResourceAction> {
        self.inner.receive_part(part, now)
    }

    /// Feed a hashmap-update packet from the gateway (large resources).
    pub fn feed_hmu(&mut self, hmu: &[u8], now: f64) -> Vec<ResourceAction> {
        self.inner.handle_hashmap_update(hmu, now)
    }

    /// Progress as (received_parts, total_parts).
    pub fn progress(&self) -> (usize, usize) {
        self.inner.progress()
    }

    /// True once every part is in (reassembly may begin).
    pub fn is_complete(&self) -> bool {
        self.inner.progress().0 >= self.inner.progress().1
            && self.inner.progress().1 > 0
    }

    /// Reassemble, hash-verify, and stream the payload into `store`.
    ///
    /// Returns the completion proof bytes to ACK to the gateway on success.
    pub fn assemble_with(
        &mut self,
        decrypt_fn: &dyn Fn(&[u8]) -> Result<Vec<u8>, ()>,
        store: &mut dyn Store,
    ) -> Result<Vec<u8>, ResourceError> {
        let actions = self.inner.assemble(decrypt_fn, &NoopCompressor);

        let mut proof = None;
        let mut payload = None;
        for a in actions {
            match a {
                ResourceAction::SendProof(p) => proof = Some(p),
                ResourceAction::DataReceived { data, .. } => payload = Some(data),
                ResourceAction::Failed(e) if payload.is_none() => return Err(e),
                _ => {}
            }
        }

        match (proof, payload) {
            (Some(p), Some(data)) => {
                store.append(&data);
                Ok(p)
            }
            _ => Err(resource_status_error(&self.inner.status)),
        }
    }

    /// Return the current status for diagnostics.
    pub fn status(&self) -> ResourceStatus {
        self.inner.status
    }
}

fn resource_status_error(s: &ResourceStatus) -> ResourceError {
    // The receiver sets Failed/Corrupt status with a specific error that we
    // lose by value; map by status for a stable diagnostic.
    match s {
        ResourceStatus::Corrupt => ResourceError::InvalidPart,
        ResourceStatus::Failed => ResourceError::MaxRetriesExceeded,
        _ => ResourceError::InvalidState,
    }
}

// Silence unused import if Compressor isn't named directly in this module body.
#[allow(unused)]
fn _compressor_ref(_: &dyn Compressor) {}
