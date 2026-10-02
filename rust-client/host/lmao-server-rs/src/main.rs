//! Rust LMAO server binary — bootstraps the app layer:
//!   - tonic gRPC `LMAO` service on :50051 (Send / Subscribe / GetIdentity)
//!   - axum contacts HTTP API on :8081 (GET/POST /contacts, /contacts/find)
//!   - optional NATS JetStream (subject lmao.messages.env)
//!   - contact book (rusqlite, LMAO_CONTACTS_DB)
//!
//! Env:
//!   LMAO_SERVER_DEST_HEX  server identity hash for GetIdentity (default = the
//!                         lmao.data dest the cardputer dials)
//!   LMAO_SERVER_NODE_NAME default "lmao-server"
//!   LMAO_ALLOWED_CLIENTS  comma-separated lxmf/delivery hashes (allow-list)
//!   LMAO_CONTACTS_DB      SQLite path (default "contacts.db")
//!   NATS_SERVER           default nats://localhost:4222
//!
//! When `LMAO_RNODE_PORT` is present, the RF leg starts an rns-net LoRa node
//! and installs the real `RnsMeshSender` into `AppState.mesh` (opportunistic
//! LXMF replies the Cardputer's `handle_lxmf_reply` decrypts). Without the
//! radio the app layer runs on the `LogMesh` placeholder.

use std::collections::HashSet;
use std::sync::Arc;

use lmao_server_rs::contacts;
use lmao_server_rs::delivery::{AppState, LogMesh};
use lmao_server_rs::grpc::LmaoGrpcService;
use lmao_server_rs::lma::lmao_server::LmaoServer;
use lmao_server_rs::nats::NatsPublisher;
use lmao_server_rs::store::ContactBook;

const DEFAULT_DEST_HEX: &str = "24a097043d6d7f8fe0fb188e375c4c66"; // lmao.data
const DEFAULT_CLIENT_DELIVERY: &str = "99ce32311dc37193eff4951a912f8f1b"; // Rust Cardputer

#[tokio::main]
async fn main() {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();

    // ── contact book ────────────────────────────────────────────────
    let contacts_db =
        std::env::var("LMAO_CONTACTS_DB").unwrap_or_else(|_| "contacts.db".into());
    let book = match ContactBook::open(&contacts_db) {
        Ok(b) => b,
        Err(e) => {
            log::error!("contact book unavailable at {contacts_db}: {e}");
            std::process::exit(1);
        }
    };
    log::info!("contact book ready at {contacts_db}");

    // ── identity + allow-list ───────────────────────────────────────
    let identity_hex = std::env::var("LMAO_SERVER_DEST_HEX").unwrap_or_else(|_| DEFAULT_DEST_HEX.into());
    let node_name = std::env::var("LMAO_SERVER_NODE_NAME").unwrap_or_else(|_| "lmao-server".into());

    let mut allowed: HashSet<String> = std::env::var("LMAO_ALLOWED_CLIENTS")
        .unwrap_or_default()
        .split(',')
        .filter_map(|s| {
            let t = s.trim().to_lowercase();
            (!t.is_empty()).then_some(t)
        })
        .collect();
    allowed.insert(DEFAULT_CLIENT_DELIVERY.to_string());

    // ── NATS (optional — degrade gracefully) ─────────────────────────
    let nats_url = std::env::var("NATS_SERVER").unwrap_or_else(|_| "nats://localhost:4222".into());
    let nats = NatsPublisher::connect(&nats_url).await;
    match &nats {
        Some(_) => log::info!("NATS JetStream connected at {nats_url}"),
        None => log::warn!("NATS unavailable ({nats_url}) — publishing disabled"),
    }

    // ── shared state ────────────────────────────────────────────────
    let state = Arc::new(AppState::new(
        book,
        allowed,
        identity_hex,
        node_name,
        nats,
        Box::new(LogMesh), // placeholder until the RF leg installs the real sender (below)
    ));

    // ── RF receive leg (the receiver, merged into the server) ──────────
    let rf_port = std::env::var("LMAO_RNODE_PORT").unwrap_or_else(|_| "/dev/ttyUSB0".into());
    let rf_disabled = std::env::var("LMAO_DISABLE_RF").as_deref() == Ok("1");
    // Announced identities the RF leg hears; the mesh sender recalls them to
    // encrypt + route replies back to each peer.
    let peers: lmao_server_rs::rns_mesh::KnownPeers = Default::default();
    let _rf_node = if rf_disabled || !std::path::Path::new(&rf_port).exists() {
        if !rf_disabled {
            log::info!("RF receive off ({rf_port} not present)");
        } else {
            log::info!("RF receive off (LMAO_DISABLE_RF=1)");
        }
        None
    } else {
        // Mutate the Default config so an UNSET env var keeps the struct's
        // default_source ("99ce…") instead of overriding it with "".
        let mut rf_cfg = lmao_server_rs::rf::RfConfig::default();
        rf_cfg.serial_port = rf_port.clone();
        rf_cfg.identity_hex_64b = std::env::var("LMAO_SERVER_RNS_IDENTITY_HEX").ok();
        if let Ok(v) = std::env::var("LMAO_RF_DEFAULT_SOURCE") {
            let v = v.trim().to_lowercase();
            if !v.is_empty() {
                rf_cfg.default_source = v;
            }
        }
        match lmao_server_rs::rf::start_rf_node(state.clone(), rf_cfg, peers.clone()) {
            Ok((node, mesh)) => {
                // Install the real opportunistic-LXMF sender (the replies the
                // Cardputer's `handle_lxmf_reply` waits for) — replaces the
                // log-only stub.
                let boxed: Box<dyn lmao_server_rs::delivery::MeshSender + Send + Sync> =
                    Box::new(mesh);
                *state.mesh.write() = Arc::from(boxed);
                log::info!("RF receive running on {rf_port}; mesh replies enabled");
                Some(node)
            }
            Err(e) => {
                log::warn!("RF receive failed to start on {rf_port}: {e}");
                None
            }
        }
    };

    // ── gRPC :50051 ─────────────────────────────────────────────────
    let grpc_port = std::env::var("LMAO_GRPC_PORT").unwrap_or_else(|_| "50051".into());
    let grpc_addr: std::net::SocketAddr = format!("[::]:{grpc_port}")
        .parse()
        .expect("invalid gRPC bind address");
    log::info!("gRPC LMAO service listening on {grpc_addr}");

    // ── contacts HTTP API :8081 (LMAO_CONTACTS_PORT, Python parity) ──
    let http_port = std::env::var("LMAO_CONTACTS_PORT").unwrap_or_else(|_| "8081".into());
    let http_listener = tokio::net::TcpListener::bind(format!("[::]:{http_port}"))
        .await
        .unwrap_or_else(|e| panic!("bind contacts API :{http_port}: {e}"));
    log::info!("contacts API listening on {}", http_listener.local_addr().unwrap());

    let grpc = tonic::transport::Server::builder()
        .add_service(LmaoServer::new(LmaoGrpcService {
            state: state.clone(),
        }))
        .serve_with_shutdown(grpc_addr, shutdown_signal());
    let http = axum::serve(http_listener, contacts::router(state.clone()))
        .with_graceful_shutdown(shutdown_signal());

    tokio::select! {
        r = grpc => log::info!("gRPC server exited: {r:?}"),
        r = http => log::info!("contacts server exited: {r:?}"),
    }
    log::info!("lmao-server-rs shutting down");
}

async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    log::info!("SIGINT received — shutting down");
}
