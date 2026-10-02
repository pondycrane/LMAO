//! Host-side proof of the outbound `MeshSender` seam — NO radio required.
//!
//! Two in-process rns-net nodes over a TCP transport relay mirror the real
//! split: the "server" node (identity A) with a peer table filled by its
//! `on_announce` (exactly what `RfCallbacks::on_announce` does on the RNode),
//! and a synthetic "client" node (identity B) that announces its `lxmf.delivery`
//! destination the way the Cardputer does.  `RnsMeshSender::send` then packs +
//! routes an opportunistic LXMF to B's recalled announced identity, and we
//! verify B can decrypt the packet and recover the exact LMAOEnvelope content
//! (ack + `DATA …` chart line) that the Cardputer's `handle_lxmf_reply` reads.
//!
//! This is the same code path as the live loop (Cardputer → server → reply on
//! the RNode LoRa) minus the physical PHY: announce-recall → X25519 encrypt →
//! opportunistic `node.send_packet` → remote local delivery + decryption.
//!
//! Run:  cargo test -p lmao-server-rs --test mesh_integration
//! Debug: RUST_LOG=debug cargo test -p lmao-server-rs --test mesh_integration -- --nocapture

use std::sync::mpsc;
use std::sync::Arc;
use std::time::{Duration, Instant};

use prost::Message as _;
use rns_crypto::identity::Identity;
use rns_crypto::OsRng;
use rns_net::{
    AnnouncedIdentity, Callbacks, DestHash, Destination, IdentityHash, InterfaceConfig,
    InterfaceId, NodeConfig, PacketHash, RnsNode, TcpClientConfig, TcpServerConfig, MODE_FULL,
};

use lmao_server_rs::delivery::MeshSender;
use lmao_server_rs::rns_mesh::{KnownPeers, RnsMeshSender};

const TIMEOUT: Duration = Duration::from_secs(10);
const SETTLE: Duration = Duration::from_millis(1500);
const KNOWN_DESTINATIONS_TTL: Duration = Duration::from_secs(48 * 60 * 60);

fn find_free_port() -> u16 {
    let pid = std::process::id() as u16;
    let base = 10_000 + (pid % 200) * 100;
    std::net::TcpListener::bind(("127.0.0.1", 0))
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .unwrap_or(base)
}

fn hex16(b: &[u8]) -> String {
    hex::encode(b).to_lowercase()
}

fn wait_for<T>(
    rx: &mpsc::Receiver<T>,
    timeout: Duration,
    mut pred: impl FnMut(&T) -> bool,
) -> Option<T> {
    let deadline = Instant::now() + timeout;
    loop {
        let remaining = deadline
            .checked_duration_since(Instant::now())
            .unwrap_or_default();
        if remaining.is_zero() {
            return None;
        }
        match rx.recv_timeout(remaining) {
            Ok(e) if pred(&e) => return Some(e),
            Ok(_) => {}
            Err(_) => return None,
        }
    }
}

// ── Server-side callbacks: learn announces into the peers table (the RF
//    `RfCallbacks::on_announce` analogue) + surface them to the test. ──
struct ServerCallbacks {
    peers: KnownPeers,
    tx: mpsc::Sender<AnnouncedIdentity>,
}

impl Callbacks for ServerCallbacks {
    fn on_announce(&mut self, announced: AnnouncedIdentity) {
        // Same insert key as rf.rs: lowercase dest-hash hex.
        self.peers
            .lock()
            .insert(hex16(&announced.dest_hash.0), announced.clone());
        let _ = self.tx.send(announced);
    }
    fn on_path_updated(&mut self, _dest_hash: DestHash, _hops: u8) {}
    fn on_local_delivery(&mut self, _dest_hash: DestHash, _raw: Vec<u8>, _packet_hash: PacketHash) {}
}

// ── Client-side callbacks: surface interface-up + locally delivered packets. ──
#[derive(Debug, Clone)]
enum ClientEvent {
    InterfaceUp,
    Delivery { dest_hash: DestHash, raw: Vec<u8> },
}

struct ClientCallbacks {
    tx: mpsc::Sender<ClientEvent>,
    up: bool,
}

impl Callbacks for ClientCallbacks {
    fn on_announce(&mut self, _announced: AnnouncedIdentity) {}
    fn on_path_updated(&mut self, _dest_hash: DestHash, _hops: u8) {}
    fn on_local_delivery(&mut self, dest_hash: DestHash, raw: Vec<u8>, _packet_hash: PacketHash) {
        let _ = self.tx.send(ClientEvent::Delivery { dest_hash, raw });
    }
    fn on_interface_up(&mut self, _id: InterfaceId) {
        if !self.up {
            self.up = true;
            let _ = self.tx.send(ClientEvent::InterfaceUp);
        }
    }
}

/// A no-op transport relay (third party that both nodes connect through).
struct TransportCallbacks;
impl Callbacks for TransportCallbacks {
    fn on_announce(&mut self, _announced: AnnouncedIdentity) {}
    fn on_path_updated(&mut self, _dest_hash: DestHash, _hops: u8) {}
    fn on_local_delivery(&mut self, _dest_hash: DestHash, _raw: Vec<u8>, _packet_hash: PacketHash) {}
}

fn node_cfg(identity: Identity, interfaces: Vec<InterfaceConfig>) -> NodeConfig {
    NodeConfig {
        panic_on_interface_error: true,
        transport_enabled: true,
        static_transport_identity: false,
        local_hops_delta: false,
        identity: Some(identity),
        interfaces,
        share_instance: false,
        instance_name: "default".into(),
        shared_instance_port: 37428,
        rpc_port: 0,
        cache_dir: None,
        ratchet_store: None,
        ratchet_expiry: std::time::Duration::from_secs(rns_core::constants::RATCHET_EXPIRY),
        management: Default::default(),
        probe_port: None,
        probe_addrs: vec![],
        probe_protocol: rns_core::holepunch::ProbeProtocol::Rnsp,
        direct_connect_policy: Default::default(),
        device: None,
        underlay_mark: None,
        hooks: Vec::new(),
        discover_interfaces: false,
        autoconnect_interface_mode: None,
        autoconnect_interface_gravity: 0,
        autoconnect_announces_to_internal: false,
        discovery_required_value: None,
        respond_to_probes: false,
        prefer_shorter_path: false,
        max_paths_per_destination: 1,
        packet_hashlist_max_entries: rns_core::constants::HASHLIST_MAXSIZE,
        packet_hashlist_allocation: rns_core::transport::types::PacketHashlistAllocation::Eager,
        max_discovery_pr_tags: rns_core::constants::MAX_PR_TAGS,
        max_path_destinations: usize::MAX,
        max_tunnel_destinations_total: usize::MAX,
        known_destinations_ttl: KNOWN_DESTINATIONS_TTL,
        known_destinations_max_entries: 8192,
        announce_table_ttl: Duration::from_secs(rns_core::constants::ANNOUNCE_TABLE_TTL as u64),
        announce_table_max_bytes: rns_core::constants::ANNOUNCE_TABLE_MAX_BYTES,
        driver_event_queue_capacity: rns_net::event::DEFAULT_EVENT_QUEUE_CAPACITY,
        interface_writer_queue_capacity: rns_net::interface::DEFAULT_ASYNC_WRITER_QUEUE_CAPACITY,
        announce_rate_defaults: rns_net::AnnounceRateDefaults::default(),
        ingress_control_defaults: rns_core::transport::types::IngressControlConfig::enabled(),
        backbone_peer_pool: None,
        announce_sig_cache_enabled: true,
        announce_sig_cache_max_entries: rns_core::constants::ANNOUNCE_SIG_CACHE_MAXSIZE,
        announce_sig_cache_ttl: Duration::from_secs(
            rns_core::constants::ANNOUNCE_SIG_CACHE_TTL as u64,
        ),
        registry: None,
        #[cfg(feature = "hooks")]
        provider_bridge: None,
    }
}

fn transport_node(port: u16) -> RnsNode {
    RnsNode::start(
        node_cfg(
            Identity::new(&mut OsRng),
            vec![InterfaceConfig {
                name: String::new(),
                type_name: "TCPServerInterface".to_string(),
                config_data: Box::new(TcpServerConfig {
                    name: "Transport TCP".into(),
                    listen_ip: "127.0.0.1".into(),
                    listen_port: port,
                    interface_id: InterfaceId(1),
                    max_connections: None,
                    ..TcpServerConfig::default()
                }),
                mode: MODE_FULL,
                gravity: 0,
                recursive_prs: false,
                announces_from_internal: true,
                announces_to_internal: None,
                ingress_control: rns_core::transport::types::IngressControlConfig::enabled(),
                ifac: None,
                discovery: None,
            }],
        ),
        Box::new(TransportCallbacks),
    )
    .expect("transport node start")
}

fn client_node(port: u16, identity: &Identity, callbacks: Box<dyn Callbacks>) -> RnsNode {
    RnsNode::start(
        node_cfg(
            Identity::from_private_key(&identity.get_private_key().unwrap()),
            vec![InterfaceConfig {
                name: String::new(),
                type_name: "TCPClientInterface".to_string(),
                config_data: Box::new(TcpClientConfig {
                    name: "Client TCP".into(),
                    target_host: "127.0.0.1".into(),
                    target_port: port,
                    interface_id: InterfaceId(1),
                    ..Default::default()
                }),
                mode: MODE_FULL,
                gravity: 0,
                recursive_prs: false,
                announces_from_internal: true,
                announces_to_internal: None,
                ingress_control: rns_core::transport::types::IngressControlConfig::enabled(),
                ifac: None,
                discovery: None,
            }],
        ),
        callbacks,
    )
    .expect("client node start")
}

fn decrypt(raw: &[u8], identity: &Identity) -> Option<Vec<u8>> {
    let packet = rns_core::packet::RawPacket::unpack(raw).ok()?;
    identity.decrypt(&packet.data).ok()
}

#[tokio::test]
async fn mesh_sender_delivers_opportunistic_lxmf_reply() {
    let _ = env_logger::try_init();

    let port = find_free_port();
    let transport = transport_node(port);

    // Server identity A.
    let a_identity = Identity::new(&mut OsRng);
    let a_pk = a_identity.get_private_key().unwrap();

    // Client identity B + its lxmf.delivery destination (Cardputer-shaped).
    let b_identity = Identity::new(&mut OsRng);
    let b_dest = Destination::single_in("lxmf", &["delivery"], IdentityHash(*b_identity.hash()));

    // Server node: peers table learned by on_announce, exactly like rf.rs.
    let peers: KnownPeers = Default::default();
    let (a_tx, a_rx) = mpsc::channel();
    let a_node = client_node(
        port,
        &a_identity,
        Box::new(ServerCallbacks {
            peers: peers.clone(),
            tx: a_tx,
        }),
    );

    // Client node.
    let (b_tx, b_rx) = mpsc::channel();
    let b_node = client_node(
        port,
        &b_identity,
        Box::new(ClientCallbacks {
            tx: b_tx,
            up: false,
        }),
    );
    b_node
        .register_destination_with_proof(&b_dest, Some(b_identity.get_private_key().unwrap()))
        .unwrap();

    // The client connects before the announce can travel.
    wait_for(&b_rx, TIMEOUT, |e| matches!(e, ClientEvent::InterfaceUp)).expect("client up");

    // The client announces lxmf.delivery (like the firmware's ~10 s cadence);
    // retry until the server node's on_announce has recalled it.
    let mut b_announced = None;
    for _ in 0..10 {
        let _ = b_node.announce(&b_dest, &b_identity, None);
        if let Some(a) = wait_for(&a_rx, Duration::from_secs(2), |_| true) {
            b_announced = Some(a);
            break;
        }
    }
    let b_announced = b_announced
        .expect("server never recalled the client's announce after retries");
    assert_eq!(
        b_announced.dest_hash,
        b_dest.hash,
        "recalled announce must be the client's lxmf.delivery destination"
    );

    // The RnsMeshSender looks peers up by the dest-hash hex the way rf.rs inserted it.
    let b_hash_hex = hex16(&b_dest.hash.0);
    assert!(peers.lock().contains_key(&b_hash_hex));

    std::thread::sleep(SETTLE);

    // The seam: pack + send an opportunistic LXMF "p:Envelope" to the client —
    // the content is an LMAOEnvelope{text} (ack + DATA line) like delivery.rs builds.
    use lma_wire::{lmao_envelope, LmaoEnvelope, TextMessage};
    let content = LmaoEnvelope {
        payload: Some(lmao_envelope::Payload::Text(TextMessage {
            node_id: "99ce32311dc37193eff4951a912f8f1b".into(),
            content: "OK\nDATA 1234.5,0.123,0.456".into(),
            timestamp: 1_800_000_000_000,
        })),
    }
    .encode_to_vec();

    let mesh = RnsMeshSender::new(
        Arc::new(a_node),
        peers.clone(),
        Identity::from_private_key(&a_pk),
    );

    mesh.send(&b_hash_hex, &content, "p:Envelope")
        .await
        .expect("send");

    // The client must receive + decrypt the reply.
    let delivery = wait_for(&b_rx, TIMEOUT, |e| matches!(e, ClientEvent::Delivery { .. }))
        .expect("client never received the opportunistic reply");
    let (dh, raw) = match delivery {
        ClientEvent::Delivery { dest_hash, raw } => (dest_hash, raw),
        _ => unreachable!(),
    };
    assert_eq!(dh, b_dest.hash, "reply addressed to the client's lxmf.delivery");

    let decrypted = decrypt(&raw, &b_identity).expect("client could not decrypt the reply");
    assert!(
        decrypted
            .windows(content.len())
            .any(|w| w == content.as_slice()),
        "decrypted reply must carry the exact LMAOEnvelope content (DATA line) the Cardputer consumes"
    );

    b_node.shutdown();
    transport.shutdown();
}
