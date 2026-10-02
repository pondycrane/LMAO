//! Real rns-net mesh dispatcher — the `delivery::MeshSender` seam wired to the
//! RF receive node (port of the Python server's `router.handle_outbound` +
//! `LXMessage(desired_method=OPPORTUNISTIC)` reply path).
//!
//! The Cardputer firmware sends its LXMF messages to the server over its own
//! radio; this is the server's mirrored half: pack an LXMF message to the
//! peer's `lxmf.delivery` destination (recalled from its on-mesh announce,
//! which carries the peer's public identity key), X25519-encrypt it to that
//! key via the node, and route it opportunistically over the RNode LoRa.  The
//! client's `comms.rs/rns_link.handle_lxmf_reply` decrypts it, sees the
//! piggybacked `DATA …` line, and paints the Sprout chart.

use std::collections::HashMap;
use std::sync::Arc;

use async_trait::async_trait;
use parking_lot::Mutex;
use rns_core::destination as rns_dest;
use rns_crypto::identity::Identity;
use rns_net::{AnnouncedIdentity, Destination, RnsNode};

use crate::delivery::MeshSender;

/// Latest announced identity per lowercase dest-hash hex.  The RF leg's
/// `on_announce` fills this when a client announces `lxmf.delivery`/`lmao.*`
/// on the mesh (~every 10 s), so outbound encryption has the peer's pubkey
/// without any config copy (the Python server's `source_dest` analogue).
pub type KnownPeers = Arc<Mutex<HashMap<String, AnnouncedIdentity>>>;

/// Opportunistic LXMF dispatcher over the RF (RNode LoRa) node.
pub struct RnsMeshSender {
    node: Arc<RnsNode>,
    peers: KnownPeers,
    identity: Identity,
}

impl RnsMeshSender {
    pub fn new(node: Arc<RnsNode>, peers: KnownPeers, identity: Identity) -> Self {
        Self {
            node,
            peers,
            identity,
        }
    }
}

#[async_trait]
impl MeshSender for RnsMeshSender {
    async fn send(&self, dest_hash: &str, content: &[u8], title: &str) -> Result<(), String> {
        let key = dest_hash.to_lowercase();
        let announced = self
            .peers
            .lock()
            .get(&key)
            .cloned()
            .ok_or_else(|| {
                format!("no announce for {key} yet (awaiting the node's lxmf.delivery announce)")
            })?;

        // The client's `lxmf.delivery` OUT destination (recalled identity hash
        // + public key) — the node encrypts to it in `send_packet`.
        let dest = Destination::single_out("lxmf", &["delivery"], &announced);

        // Our own delivery hash is the LXMF source (the reply's From), exactly
        // like the firmware's `build_message_to_server` uses the server dest.
        let src = rns_dest::destination_hash("lxmf", &["delivery"], Some(self.identity.hash()));

        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);

        // Pack the LXMF message (title = p:Envelope, content = the LMAOEnvelope
        // TextMessage with the ACK + `DATA …` line) — mirrors the Cardputer's
        // `build_lxmf_to_server`, reversed.
        let packed = lxmf_core::message::pack(
            &dest.hash.0,
            &src,
            now,
            title.as_bytes(),
            content,
            Vec::new(),
            None,
            |data| {
                self.identity
                    .sign(data)
                    .map_err(|_| lxmf_core::message::Error::SignError)
            },
        )
        .map_err(|e| format!("lxmf pack: {e:?}"))?
        .packed;

        self.node
            .send_packet(&dest, &packed)
            .map_err(|e| format!("send_packet: {e:?}"))?;
        log::info!(
            "rfc mesh reply {dest_hash} ({title}, {}B) enqueued on LoRa",
            content.len()
        );
        Ok(())
    }
}
