//! rns-net RF receive leg — the Rust server's own LoRa/LXMF ingest capability.
//!
//! This IS the receiver, merged into the server (port of the standalone
//! `lmao-rns-receiver`): `lmao-server` starts an rns-net node on the RNode
//! LoRa serial interface, accepts inbound Links dialed to the server's
//! `lmao.data` destination, reassembles payload Resources and feeds each one
//! into [`crate::delivery::AppState::handle_delivery`] — so the receive path
//! and the app layer are one binary / one image / one process.
//!
//! The DeliveryHandler allow-list gates the source (the remote's 16-byte
//! identity/delivery hash from `on_remote_identified`), learns the contact,
//! folds the Sprout chart, ACKs, and publishes to NATS — the full Python
//! `handle_lxmf_delivery` semantics.

use std::collections::HashMap;
use std::sync::Arc;

use rns_net::{
    Callbacks, DestHash, InterfaceConfig, InterfaceId, LinkId, NodeConfig, PacketHash,
    RNodeConfig, RNodeSubConfig, RnsNode, MODE_FULL,
};
use rns_crypto::identity::Identity;

use crate::delivery::SharedState;

/// Radio + identity settings for the RF receive leg.
pub struct RfConfig {
    pub serial_port: String,
    /// 128-hex-char Ed25519 private key of the LMAO server identity (the
    /// cardputer bakes this identity's dest + Ed25519 pub). None = random.
    pub identity_hex_64b: Option<String>,
    /// Server `lmao.data` destination hash (16 bytes) — the cardputer's baked target.
    pub lma_data_hash: [u8; 16],
    pub frequency: u32,
    pub bandwidth: u32,
    pub spreading_factor: u8,
    pub coding_rate: u8,
    /// Fallback source delivery hash for links whose peer never RNS-identifies
    /// (MicroPython/LXMF peers identify at the LXMF layer, not RNS-native).
    /// Must be in `LMAO_ALLOWED_CLIENTS` or the DeliveryHandler gate drops it.
    pub default_source: String,
}

impl Default for RfConfig {
    fn default() -> Self {
        Self {
            serial_port: "/dev/ttyUSB0".into(),
            identity_hex_64b: None,
            lma_data_hash: [
                0x24, 0xa0, 0x97, 0x04, 0x3d, 0x6d, 0x7f, 0x8f, 0xe0, 0xfb, 0x18, 0x8e, 0x37,
                0x5c, 0x4c, 0x66,
            ],
            frequency: 868_000_000,
            bandwidth: 125_000,
            spreading_factor: 7,
            coding_rate: 5,
            default_source: "99ce32311dc37193eff4951a912f8f1b".into(), // Rust Cardputer
        }
    }
}

/// log-friendly hex of a 16-byte source hash (unknown = all-zero placeholder).
fn hex16(b: &[u8]) -> String {
    hex::encode(b)
}

/// rns-net callback sink: recalls each Link's remote source delivery hash and
/// pushes received Resource payloads into the DeliveryHandler.
pub struct RfCallbacks {
    state: SharedState,
    sources: HashMap<LinkId, String>,
    default_source: String,
    /// Tokio runtime handle — rns-net callbacks run on the `rns-driver`
    /// thread (NOT a tokio worker), so `tokio::spawn` would panic. Spawn via
    /// the shared handle instead.
    rt: tokio::runtime::Handle,
}

impl RfCallbacks {
    pub fn new(state: SharedState, default_source: String, rt: tokio::runtime::Handle) -> Self {
        Self {
            state,
            sources: HashMap::new(),
            default_source,
            rt,
        }
    }
}

impl Callbacks for RfCallbacks {
    fn on_announce(&mut self, announced: rns_net::AnnouncedIdentity) {
        log::debug!("RF announce dest={} hops={}", announced.dest_hash, announced.hops);
    }
    fn on_path_updated(&mut self, dest_hash: DestHash, hops: u8) {
        log::debug!("RF path updated dest={} hops={}", dest_hash, hops);
    }
    fn on_interface_up(&mut self, id: InterfaceId) {
        log::info!("RF interface up id={id:?}");
    }
    fn on_interface_down(&mut self, id: InterfaceId) {
        log::warn!("RF interface down id={id:?}");
    }
    fn on_link_established(
        &mut self,
        link_id: LinkId,
        dest_hash: DestHash,
        rtt: f64,
        is_initiator: bool,
    ) {
        log::info!(
            "RF LINK established link={link_id:?} dest={dest_hash} rtt={rtt} initiator={is_initiator}"
        );
    }
    fn on_remote_identified(&mut self, link_id: LinkId, identity_hash: rns_core::types::IdentityHash, _pk: [u8; 64]) {
        let source = hex16(&identity_hash.0);
        log::info!("RF remote identified link={link_id:?} source_hash={source}");
        self.sources.insert(link_id, source);
    }
    fn on_local_delivery(&mut self, dest_hash: DestHash, _raw: Vec<u8>, _ph: PacketHash) {
        log::debug!("RF local delivery dest={dest_hash}");
    }
    fn on_resource_accept_query(
        &mut self,
        link_id: LinkId,
        resource_hash: Vec<u8>,
        transfer_size: u64,
        has_metadata: bool,
    ) -> bool {
        log::info!(
            "RF RESOURCE advertised link={link_id:?} hash={} size={transfer_size} has_metadata={has_metadata}",
            hex::encode(&resource_hash)
        );
        true
    }
    fn on_resource_progress(&mut self, link_id: LinkId, received: usize, total: usize) {
        log::debug!("RF resource progress link={link_id:?} {received}/{total} bytes");
    }
    fn on_resource_received(&mut self, link_id: LinkId, data: Vec<u8>, metadata: Option<Vec<u8>>) {
        let digest = rns_crypto::sha256::sha256(&data);
        let meta = metadata.map(|m| m.len()).unwrap_or(0);
        let source = self
            .sources
            .get(&link_id)
            .cloned()
            .unwrap_or_else(|| self.default_source.clone());
        if !self.sources.contains_key(&link_id) {
            log::info!(
                "RF RESOURCE link={link_id:?} — peer not RNS-identified; using configured RF default source"
            );
        }
        log::info!(
            "RF RESOURCE received link={link_id:?} bytes={} sha256={} metadata_bytes={meta} source={source}",
            data.len(),
            hex::encode(digest)
        );
        let state = self.state.clone();
        self.rt.spawn(async move {
            state.handle_delivery(&source, &data, "p:Envelope").await;
        });
    }
    fn on_resource_failed(&mut self, link_id: LinkId, error: String) {
        log::warn!("RF RESOURCE failed link={link_id:?} err={error}");
    }
}

/// rns-net RNode (LoRa) interface config for `RfConfig`.
fn rnode_config(cfg: &RfConfig) -> RNodeConfig {
    let sub = RNodeSubConfig {
        name: "LoRa868".into(),
        vport: 0,
        outgoing: true,
        frequency: cfg.frequency,
        bandwidth: cfg.bandwidth,
        txpower: 7,
        spreading_factor: cfg.spreading_factor,
        coding_rate: cfg.coding_rate,
        flow_control: false,
        st_alock: None,
        lt_alock: None,
    };
    RNodeConfig {
        name: format!("RNode {}", cfg.serial_port),
        port: cfg.serial_port.clone(),
        speed: 115_200,
        base_interface_id: InterfaceId(1),
        subinterfaces: vec![sub.clone()],
        multi: false,
        id_interval: None,
        id_callsign: None,
        pre_opened_fd: None,
        underlay_mark: None,
        runtime: Arc::new(std::sync::Mutex::new(
            rns_net::interface::rnode::RNodeRuntime { sub, writer: None },
        )),
    }
}

/// Start the RF receive node (blocking). Returns the running [`RnsNode`].
pub fn start_rf_node(state: SharedState, cfg: RfConfig) -> Result<RnsNode, Box<dyn std::error::Error>> {
    let identity = match &cfg.identity_hex_64b {
        Some(hx) => {
            let raw = hex::decode(hx)?;
            let pk: [u8; 64] = raw.as_slice().try_into().map_err(|_| "identity hex must be 64 bytes")?;
            Identity::from_private_key(&pk)
        }
        None => Identity::new(&mut rns_crypto::OsRng),
    };
    let pk = identity.get_private_key().ok_or("no private key")?;
    let pb = identity.get_public_key().ok_or("no public key")?;
    let sig_prv: [u8; 32] = pk[32..].try_into().unwrap();
    let sig_pub: [u8; 32] = pb[32..].try_into().unwrap();

    log::info!("LMAO server ed25519 pub = {}", hex::encode(&sig_pub));

    let rnode = rnode_config(&cfg);
    let node = RnsNode::start(
        NodeConfig {
            panic_on_interface_error: false,
            transport_enabled: false,
            static_transport_identity: false,
            local_hops_delta: false,
            identity: Some(identity),
            interfaces: vec![InterfaceConfig {
                name: String::new(),
                type_name: "RNodeInterface".to_string(),
                config_data: Box::new(rnode),
                mode: MODE_FULL,
                gravity: 0,
                recursive_prs: false,
                announces_from_internal: true,
                announces_to_internal: None,
                ingress_control: rns_core::transport::types::IngressControlConfig::disabled(),
                ifac: None,
                discovery: None,
            }],
            // Full field set matches the RF-reliable standalone `lmao-rns-receiver`
            // verbatim (NOT ..Default::default(), which gave unstable links).
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
            known_destinations_ttl: std::time::Duration::from_secs(48 * 60 * 60),
            known_destinations_max_entries: 8192,
            announce_table_ttl: std::time::Duration::from_secs(
                rns_core::constants::ANNOUNCE_TABLE_TTL as u64,
            ),
            announce_table_max_bytes: rns_core::constants::ANNOUNCE_TABLE_MAX_BYTES,
            driver_event_queue_capacity: rns_net::event::DEFAULT_EVENT_QUEUE_CAPACITY,
            interface_writer_queue_capacity: rns_net::interface::DEFAULT_ASYNC_WRITER_QUEUE_CAPACITY,
            announce_rate_defaults: rns_net::AnnounceRateDefaults::default(),
            ingress_control_defaults: rns_core::transport::types::IngressControlConfig::enabled(),
            announce_sig_cache_enabled: true,
            announce_sig_cache_max_entries: rns_core::constants::ANNOUNCE_SIG_CACHE_MAXSIZE,
            announce_sig_cache_ttl: std::time::Duration::from_secs(
                rns_core::constants::ANNOUNCE_SIG_CACHE_TTL as u64,
            ),
            registry: None,
            backbone_peer_pool: None,
        },
        Box::new(RfCallbacks::new(
            state,
            cfg.default_source,
            tokio::runtime::Handle::current(),
        )),
    )?;
    node.register_link_destination(cfg.lma_data_hash, sig_prv, sig_pub, 2)?;
    log::info!(
        "RF receive listening on RNode {} for lmao.data dest {}",
        cfg.serial_port,
        hex::encode(cfg.lma_data_hash)
    );
    Ok(node)
}
