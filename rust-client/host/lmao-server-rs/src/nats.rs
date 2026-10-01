//! NATS JetStream publisher — Rust port of `lmao_server.Server._publish_to_nats`
//! + `_ensure_nats_connected` (issue #85): fire-and-forget publish of each
//! incoming LXMF payload to subject `lmao.messages.env` on stream
//! `LMAO_MESSAGES`; a dead NATS degrades gracefully, never crashes delivery.

use async_nats::jetstream::Context;
use async_nats::Client;

const NATS_SUBJECT: &str = "lmao.messages.env";
const NATS_STREAM: &str = "LMAO_MESSAGES";
const NATS_STREAM_SUBJECTS: &[&str] = &["lmao.messages.>"];

pub struct NatsPublisher {
    _client: Client,
    js: Context,
}

impl NatsPublisher {
    /// Connect and ensure the JetStream stream exists (mirrors the Python
    /// supervisor's stream bootstrap). None if NATS is unreachable — the app
    /// layer must keep running without it.
    pub async fn connect(url: &str) -> Option<Self> {
        let client = async_nats::connect(url).await.ok()?;
        let js = async_nats::jetstream::new(client.clone());
        let cfg = async_nats::jetstream::stream::Config {
            name: NATS_STREAM.into(),
            subjects: NATS_STREAM_SUBJECTS.iter().map(|s| s.to_string()).collect(),
            ..Default::default()
        };
        // create_stream if absent (a stream that exists errors; update tolerates).
        if js.get_stream(NATS_STREAM).await.is_err() {
            if let Err(e) = js.create_stream(cfg).await {
                log::warn!("NATS stream create failed: {e}");
            }
        }
        Some(Self {
            _client: client,
            js,
        })
    }

    /// Fire-and-forget publish. No failure path raises.
    pub async fn publish(&self, data: &[u8]) {
        if let Err(e) = self.js.publish(NATS_SUBJECT, data.to_vec().into()).await {
            log::warn!("NATS publish failed: {e}");
        } else {
            log::debug!("published {} bytes to NATS {NATS_SUBJECT}", data.len());
        }
    }

    /// Exposed for tests: the subject constant.
    pub fn subject() -> &'static str {
        NATS_SUBJECT
    }
}

#[allow(dead_code)]
fn _stream_namer() -> String {
    NATS_STREAM.into()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn subject_matches_python() {
        assert_eq!(NatsPublisher::subject(), "lmao.messages.env");
    }
}
