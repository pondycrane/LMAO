//! Flash-persistence boundary for a received/tx'd resource payload.
//!
//! The design targets *streamed* chunks to flash (never the whole payload in
//! RAM). `rns-core`'s receiver holds raw (encrypted) parts on the heap, so the
//! `Store` trait is the seam where the leaf would land the streamed/decrypted
//! bytes; host tests use an in-memory `VecBackedStore`, and a real firmware
//! leg plugs an NVS/flash backend in.

use alloc::vec::Vec;

/// Where a resource's reassembled bytes land — the flash-persistence boundary.
pub trait Store {
    /// Append a reassembled chunk to the store.
    fn append(&mut self, chunk: &[u8]);
    /// Materialise the full written payload (host/verify; a flash backend
    /// returns a handle instead of a whole Vec).
    fn take(self) -> Vec<u8>;
}

/// In-memory store (host tests).
pub struct VecBackedStore {
    data: Vec<u8>,
}

impl VecBackedStore {
    pub fn new() -> Self {
        VecBackedStore { data: Vec::new() }
    }
}

impl Default for VecBackedStore {
    fn default() -> Self {
        Self::new()
    }
}

impl Store for VecBackedStore {
    fn append(&mut self, chunk: &[u8]) {
        self.data.extend_from_slice(chunk);
    }
    fn take(self) -> Vec<u8> {
        self.data
    }
}
