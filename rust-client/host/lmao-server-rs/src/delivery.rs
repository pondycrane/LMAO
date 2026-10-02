//! LXMF delivery-handler app logic — Rust port of
//! `lmao_server.Server.handle_lxmf_delivery` (issue #151).
//!
//! The RNS/LXMF mesh sits behind the [`MeshSender`] seam so the whole pipeline
//! is host-testable; `main()` supplies the real source (the rns-net RF
//! link/resource receiver) and a mesh dispatcher.

use std::collections::HashSet;
use std::sync::Arc;

use async_trait::async_trait;
use lma_wire::{lmao_envelope, LmaoEnvelope, TextMessage};
use parking_lot::Mutex;
use prost::Message;
use tokio::sync::mpsc;

use crate::nats::NatsPublisher;
use crate::sprout::SharedSproutHistory;
use crate::store::ContactBook;

/// A message pushed to a gRPC `Subscribe` stream (Python fans out the same
/// fields: envelope bytes + source hash + title).
#[derive(Debug, Clone)]
pub struct DeliveryEvent {
    pub envelope: Vec<u8>,
    pub source_hash: String,
    pub title: String,
}

/// Mesh send seam (e.g. LXMF message dispatch to a destination identity hash).
#[async_trait]
pub trait MeshSender: Send + Sync {
    /// Send `content` (a serialized LMAOEnvelope) to the destination identity
    /// hash, titled `title`; Err on unreachable/invalid (no panic).
    async fn send(&self, dest_hash: &str, content: &[u8], title: &str) -> Result<(), String>;
}

/// Logs-only mesh used until a real rns-net/LXMF dispatcher is wired in.
#[derive(Default)]
pub struct LogMesh;
#[async_trait]
impl MeshSender for LogMesh {
    async fn send(&self, dest_hash: &str, content: &[u8], title: &str) -> Result<(), String> {
        log::info!(
            "[mesh-stub] send -> {dest_hash} title={title} bytes={}",
            content.len()
        );
        Ok(())
    }
}

/// Shared server state behind `Arc` — the single object tonic + axum +
/// delivery all touch.
pub struct AppState {
    pub contact_book: ContactBook,
    pub allowed_clients: HashSet<String>,
    pub server_identity_hex: String,
    pub node_name: String,
    pub subscribers: Mutex<Vec<mpsc::UnboundedSender<DeliveryEvent>>>,
    pub sprout: SharedSproutHistory,
    pub nats: Option<NatsPublisher>,
    /// Late-bound mesh dispatcher (LogMesh until the RF node is up, then the
    /// real rns-net sender) — `RwLock<Arc<..>>` so `main()` can install the
    /// real one after `start_rf_node` returns its node, and callers can clone
    /// the handle out of the guard (no guard held across an `.await`).
    pub mesh: Arc<parking_lot::RwLock<Arc<dyn MeshSender + Send + Sync>>>,
}

impl AppState {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        contact_book: ContactBook,
        allowed_clients: HashSet<String>,
        server_identity_hex: String,
        node_name: String,
        nats: Option<NatsPublisher>,
        mesh: Box<dyn MeshSender + Send + Sync>,
    ) -> Self {
        Self {
            contact_book,
            allowed_clients,
            server_identity_hex,
            node_name,
            subscribers: Mutex::new(Vec::new()),
            sprout: SharedSproutHistory::default(),
            nats,
            mesh: Arc::new(parking_lot::RwLock::new(Arc::from(mesh))),
        }
    }

    pub fn register_subscriber(&self, tx: mpsc::UnboundedSender<DeliveryEvent>) {
        self.subscribers.lock().push(tx);
    }

    pub fn unregister_subscriber(&self, tx: &mpsc::UnboundedSender<DeliveryEvent>) {
        self.subscribers.lock().retain(|s| !std::ptr::eq(s, tx));
    }

    /// `handle_lxmf_delivery` port: allow-list gate → learn contact → fold
    /// Sprout chart → ACK reply (piggybacked DATA line) → NATS → gRPC fan-out.
    pub async fn handle_delivery(&self, source_hash: &str, content: &[u8], title: &str) {
        let key = source_hash.to_lowercase();
        if !self.allowed_clients.contains(&key) {
            log::warn!(
                "dropping LXMF from unauthorized node {source_hash} (not in allow-list) — \
                 add its lxmf/delivery hash to LMAO_ALLOWED_CLIENTS"
            );
            return;
        }

        // Contact book (issue #151): auto-learn or touch so the server can
        // reach this device before/without an operator naming it.
        if self.contact_book.is_known(&key).unwrap_or(false) {
            let _ = self.contact_book.touch(&key);
        } else if let Err(e) = self.contact_book.register(&key, "device", None, None, None) {
            log::warn!("contact book: register failed for {}: {e}", &key[..key.len().min(12)]);
        } else {
            log::info!("contact book: learned new device {}", &key[..key.len().min(12)]);
        }

        // Fold Sprout telemetry into the chart buffer (only real Sprout
        // reports — those carrying a soil-moisture reading — own the chart).
        if let Ok(envelope) = LmaoEnvelope::decode(content) {
            if let Some(lmao_envelope::Payload::Sensor(report)) = &envelope.payload {
                let samples: Vec<(u32, f32)> = report
                    .readings
                    .iter()
                    .map(|r| (r.sensor_id, r.value))
                    .collect();
                self.sprout.fold_sensor(&report.node_id, samples);
            }
        } else {
            log::debug!("payload is not an LMAOEnvelope — treating as raw text");
        }

        // ACK reply (protobuf TextMessage) with the chart DATA line piggybacked.
        let reply_text = format!(
            "ACK from LMAO Server — received your message ({} bytes)",
            content.len()
        );
        let data_line = self.sprout.data_line();
        let reply_text = if data_line.is_empty() {
            reply_text
        } else {
            format!("{reply_text}\n{data_line}")
        };

        let reply = LmaoEnvelope {
            payload: Some(lmao_envelope::Payload::Text(TextMessage {
                node_id: source_hash.to_owned(),
                content: reply_text.clone(),
                timestamp: now_ms(),
                ..Default::default()
            })),
        };
        let reply_bytes = reply.encode_to_vec();
        let mesh = self.mesh.read().clone();
        if let Err(e) = mesh.send(source_hash, &reply_bytes, "p:Envelope").await {
            log::warn!("ACK to {source_hash} failed: {e}");
        } else {
            log::info!("reply sent to {source_hash}: {reply_text:?}");
        }

        // NATS JetStream (fire-and-forget; a dead NATS must never crash delivery).
        if let Some(nats) = &self.nats {
            nats.publish(content).await;
        } else {
            log::debug!("NATS unavailable — skipping publish");
        }

        // gRPC subscriber fan-out.
        self.fanout(DeliveryEvent {
            envelope: content.to_vec(),
            source_hash: key,
            title: title.to_owned(),
        });
    }

    pub fn fanout(&self, ev: DeliveryEvent) {
        let mut subs = self.subscribers.lock();
        subs.retain(|tx| tx.send(ev.clone()).is_ok());
    }
}

fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// Convenience for `Arc<AppState>` alias used by tonic/axum handlers.
pub type SharedState = Arc<AppState>;

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::ContactBook;

    fn make_state(allowed: &[&str]) -> Arc<AppState> {
        let book = ContactBook::open_in_memory().unwrap();
        Arc::new(AppState::new(
            book,
            allowed.iter().map(|s| s.to_lowercase()).collect(),
            "24a097043d6d7f8fe0fb188e375c4c66".into(),
            "lmao-server".into(),
            None,
            Box::new(LogMesh),
        ))
    }

    fn allowed_hash() -> String {
        "99ce32311dc37193eff4951a912f8f1b".into()
    }

    #[tokio::test]
    async fn unauthorized_source_is_dropped() {
        let state = make_state(&[]);
        state
            .handle_delivery("99ce32311dc37193eff4951a912f8f1b", b"hello", "p:Envelope")
            .await;
        assert!(state.contact_book.all().unwrap().is_empty());
    }

    #[tokio::test]
    async fn authorized_source_is_learned_and_acknowledged() {
        let state = make_state(&[&allowed_hash()]);
        // Text payload (fits the raw fallback path).
        state
            .handle_delivery(&allowed_hash(), b"ping", "p:Envelope")
            .await;
        let contacts = state.contact_book.all().unwrap();
        assert_eq!(contacts.len(), 1);
        assert_eq!(contacts[0].delivery_hash, allowed_hash());
        assert_eq!(contacts[0].device_type, "device");
    }

    #[tokio::test]
    async fn sensor_report_updates_sprout_chart() {
        use lma_wire::{sensor_envelope, SensorReading};
        let state = make_state(&[&allowed_hash()]);
        let env = sensor_envelope(
            "sprout01",
            1,
            3.3,
            vec![SensorReading {
                sensor_id: 4,
                value: 40.0,
                unit: "%".into(),
                timestamp_ms: 1,
            }],
        );
        state
            .handle_delivery(&allowed_hash(), &env.encode_to_vec(), "p:Envelope")
            .await;
        let line = state.sprout.data_line();
        assert!(line.starts_with("DATA sprout01"), "got: {line}");
    }

    #[tokio::test]
    async fn subscribers_receive_fanout() {
        let state = make_state(&[&allowed_hash()]);
        let (tx, mut rx) = mpsc::unbounded_channel();
        state.register_subscriber(tx);
        state
            .handle_delivery(&allowed_hash(), b"hi", "p:Envelope")
            .await;
        let ev = rx.try_recv().expect("fanout event");
        assert_eq!(ev.source_hash, allowed_hash());
        assert_eq!(ev.envelope, b"hi");
    }
}
