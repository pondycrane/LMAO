//! T0 DECISION GATE harness.
//!
//! A std desktop Rust RNS node (`rns-net`) that establishes a **Link** to a
//! Python RNS peer and runs an RNS **Resource** in both directions over a
//! shared mesh (TCP interface), verifying chunk reassembly + digest and
//! sender-side completion proof.
//!
//! This is the substantive protocol-interop shadow of design §2 / ticket T0:
//! it proves the exact protocol stack the production `lmao-server` runs
//! (Python RNS) talks to the Rust protocol core bidirectionally — announce →
//! path → link → resource. The production RF-live leg additionally needs a
//! radio (see `--rnode`).
//!
//! Usage:
//!   LMAO_PYTHON=<python-with-rns-1.3.5> lmao-t0-interop          # TCP interop gate
//!   lmao-t0-interop --rnode /dev/ttyUSB0 [868.0]                # live RNode radio session
//!
//! Exit: 0 = gate PASS, 1 = gate FAIL, 2 = hardware/usage unavailable.

use std::io::{BufRead, BufReader, Write};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc::{self, Receiver, Sender};
use std::time::{Duration, Instant};

use rns_core::packet::RawPacket;
use rns_core::types::{DestHash, IdentityHash, LinkId, PacketHash};
use rns_crypto::identity::Identity;
use rns_crypto::OsRng;
use rns_net::{
    AnnouncedIdentity, Callbacks, Destination, InterfaceConfig, NodeConfig, RnsNode,
    TcpClientConfig, MODE_FULL,
};

const KNOWN_DESTINATIONS_TTL: Duration = Duration::from_secs(48 * 60 * 60);

const APP_NAME: &str = "interop";
const PYTHON_ASPECT: &str = "python";
const RUST_ASPECT: &str = "rust";
const RUST_TO_PYTHON_PAYLOAD: &[u8] = b"rust-to-python via python announce";
const PYTHON_TO_RUST_PAYLOAD: &[u8] = b"python-to-rust via rust announce";
const TIMEOUT: Duration = Duration::from_secs(30);

#[derive(Debug, Clone)]
enum RustEvent {
    Announce(AnnouncedIdentity),
    Delivery {
        dest_hash: DestHash,
        raw: Vec<u8>,
        packet_hash: PacketHash,
    },
    LinkEstablished {
        link_id: [u8; 16],
        is_initiator: bool,
    },
    LinkData {
        link_id: [u8; 16],
        context: u8,
        data: Vec<u8>,
    },
    ResourceReceived {
        link_id: [u8; 16],
        data: Vec<u8>,
    },
    ResourceCompleted {
        link_id: [u8; 16],
    },
}

struct TestCallbacks {
    tx: Sender<RustEvent>,
}

impl Callbacks for TestCallbacks {
    fn on_announce(&mut self, announced: AnnouncedIdentity) {
        let _ = self.tx.send(RustEvent::Announce(announced));
    }
    fn on_path_updated(&mut self, _: DestHash, _: u8) {}
    fn on_local_delivery(&mut self, dest_hash: DestHash, raw: Vec<u8>, packet_hash: PacketHash) {
        let _ = self.tx.send(RustEvent::Delivery {
            dest_hash,
            raw,
            packet_hash,
        });
    }
    fn on_link_established(
        &mut self,
        link_id: LinkId,
        _dest_hash: DestHash,
        _rtt: f64,
        is_initiator: bool,
    ) {
        let _ = self.tx.send(RustEvent::LinkEstablished {
            link_id: link_id.0,
            is_initiator,
        });
    }
    fn on_link_data(&mut self, link_id: LinkId, context: u8, data: Vec<u8>) {
        let _ = self.tx.send(RustEvent::LinkData {
            link_id: link_id.0,
            context,
            data,
        });
    }
    fn on_resource_received(&mut self, link_id: LinkId, data: Vec<u8>, _meta: Option<Vec<u8>>) {
        let _ = self.tx.send(RustEvent::ResourceReceived {
            link_id: link_id.0,
            data,
        });
    }
    fn on_resource_completed(&mut self, link_id: LinkId) {
        let _ = self.tx.send(RustEvent::ResourceCompleted { link_id: link_id.0 });
    }
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{:02x}", b)).collect()
}
fn parse_hex_16(s: &str) -> [u8; 16] {
    assert_eq!(s.len(), 32, "expected 16-byte hex string");
    let mut out = [0u8; 16];
    for i in 0..16 {
        out[i] = u8::from_str_radix(&s[i * 2..i * 2 + 2], 16).unwrap();
    }
    out
}
fn decode_hex(s: &str) -> Vec<u8> {
    assert_eq!(s.len() % 2, 0, "hex string must have even length");
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}
fn decrypt_delivery(raw: &[u8], identity: &Identity) -> Vec<u8> {
    let packet = RawPacket::unpack(raw).expect("Rust delivery should be a valid packet");
    identity
        .decrypt(&packet.data)
        .expect("Rust should decrypt Python packet")
}

fn wait_for_rust_event<F, T>(rx: &Receiver<RustEvent>, timeout: Duration, mut predicate: F) -> Option<T>
where
    F: FnMut(&RustEvent) -> Option<T>,
{
    let deadline = Instant::now() + timeout;
    loop {
        let remaining = deadline.checked_duration_since(Instant::now())?;
        match rx.recv_timeout(remaining) {
            Ok(event) => {
                if let Some(result) = predicate(&event) {
                    return Some(result);
                }
            }
            Err(_) => return None,
        }
    }
}

fn request_python_announce(
    python: &mut PythonRns,
    rust_rx: &Receiver<RustEvent>,
    expected_hash: DestHash,
    timeout: Duration,
) -> AnnouncedIdentity {
    let expected_hash_hex = hex(&expected_hash.0);
    let deadline = Instant::now() + timeout;
    loop {
        python.command("announce_py");
        python
            .try_wait_for_event(Duration::from_secs(2), |ev| {
                ev["event"] == "python_announced" && ev["dest_hash"] == expected_hash_hex
            })
            .expect("Python should acknowledge announce command");
        if let Some(a) =
            wait_for_rust_event(rust_rx, Duration::from_secs(2), |ev| match ev {
                RustEvent::Announce(a) if a.dest_hash == expected_hash => Some(a.clone()),
                _ => None,
            })
        {
            return a;
        }
        if Instant::now() >= deadline {
            panic!("timed out waiting for Rust to receive Python announce");
        }
    }
}

fn wait_for_rust_delivery(
    rx: &Receiver<RustEvent>,
    expected_hash: DestHash,
    timeout: Duration,
) -> (Vec<u8>, PacketHash) {
    wait_for_rust_event(rx, timeout, |ev| match ev {
        RustEvent::Delivery {
            dest_hash,
            raw,
            packet_hash,
        } if *dest_hash == expected_hash => Some((raw.clone(), *packet_hash)),
        _ => None,
    })
    .expect("timed out waiting for Rust local delivery callback")
}

struct PythonRns {
    child: Child,
    events: Receiver<serde_json::Value>,
}

impl PythonRns {
    fn spawn(python: &str) -> Self {
        let mut child = Command::new(python)
            .args(["-c", PYTHON_INTEROP_SCRIPT])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("failed to start Python RNS process");

        let stdout = child.stdout.take().unwrap();
        let stderr = child.stderr.take().unwrap();
        let (events_tx, events_rx) = mpsc::channel();
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                let Ok(line) = line else { break };
                match serde_json::from_str::<serde_json::Value>(&line) {
                    Ok(v) => {
                        let _ = events_tx.send(v);
                    }
                    Err(_) => eprintln!("[python] {}", line),
                }
            }
        });
        std::thread::spawn(move || {
            for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                eprintln!("[python:err] {}", line);
            }
        });
        Self {
            child,
            events: events_rx,
        }
    }
    fn command(&mut self, command: &str) {
        let stdin = self.child.stdin.as_mut().expect("python stdin available");
        writeln!(stdin, "{command}").expect("write command to Python");
        stdin.flush().expect("flush Python stdin");
    }
    fn wait_for_event<F>(&self, timeout: Duration, predicate: F) -> serde_json::Value
    where
        F: FnMut(&serde_json::Value) -> bool,
    {
        self.try_wait_for_event(timeout, predicate)
            .expect("timed out waiting for Python event")
    }
    fn try_wait_for_event<F>(&self, timeout: Duration, mut predicate: F) -> Option<serde_json::Value>
    where
        F: FnMut(&serde_json::Value) -> bool,
    {
        let deadline = Instant::now() + timeout;
        loop {
            let remaining = deadline.checked_duration_since(Instant::now())?;
            let ev = self.events.recv_timeout(remaining).ok()?;
            if predicate(&ev) {
                return Some(ev);
            }
        }
    }
}

impl Drop for PythonRns {
    fn drop(&mut self) {
        if let Some(stdin) = self.child.stdin.as_mut() {
            let _ = writeln!(stdin, "stop");
            let _ = stdin.flush();
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn start_rust_node(port: u16, tx: Sender<RustEvent>) -> RnsNode {
    RnsNode::start(
        NodeConfig {
            panic_on_interface_error: false,
            transport_enabled: false,
            static_transport_identity: false,
            local_hops_delta: false,
            identity: None,
            interfaces: vec![InterfaceConfig {
                name: String::new(),
                type_name: "TCPClientInterface".to_string(),
                config_data: Box::new(TcpClientConfig {
                    name: "interop-tcp".into(),
                    target_host: "127.0.0.1".into(),
                    target_port: port,
                    reconnect_wait: Duration::from_millis(500),
                    max_reconnect_tries: Some(3),
                    connect_timeout: Duration::from_secs(5),
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
            announce_table_ttl: std::time::Duration::from_secs(rns_core::constants::ANNOUNCE_TABLE_TTL as u64),
            announce_table_max_bytes: rns_core::constants::ANNOUNCE_TABLE_MAX_BYTES,
            driver_event_queue_capacity: rns_net::event::DEFAULT_EVENT_QUEUE_CAPACITY,
            interface_writer_queue_capacity: rns_net::interface::DEFAULT_ASYNC_WRITER_QUEUE_CAPACITY,
            announce_rate_defaults: rns_net::AnnounceRateDefaults::default(),
            ingress_control_defaults: rns_core::transport::types::IngressControlConfig::enabled(),
            backbone_peer_pool: None,
            announce_sig_cache_enabled: true,
            announce_sig_cache_max_entries: rns_core::constants::ANNOUNCE_SIG_CACHE_MAXSIZE,
            announce_sig_cache_ttl: std::time::Duration::from_secs(rns_core::constants::ANNOUNCE_SIG_CACHE_TTL as u64),
            registry: None,
        },
        Box::new(TestCallbacks { tx }),
    )
    .expect("failed to start Rust node")
}

const PYTHON_INTEROP_SCRIPT: &str = r#"
import json, hashlib, os, signal, socket, sys, tempfile

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]; sock.close()

config_dir = tempfile.mkdtemp()
with open(os.path.join(config_dir, "config"), "w") as f:
    f.write(f"""[reticulum]
  enable_transport = false
  share_instance = yes

[interfaces]
  [[TCP Server Interface]]
    type = TCPServerInterface
    interface_enabled = true
    listen_ip = 127.0.0.1
    listen_port = {port}
""")

import RNS
def emit(event, **fields):
    fields["event"] = event
    print(json.dumps(fields), flush=True)

reticulum = RNS.Reticulum(configdir=config_dir)
identity = RNS.Identity()
destination = RNS.Destination(identity, RNS.Destination.IN, RNS.Destination.SINGLE, "interop", "python")

def packet_callback(data, packet):
    emit("python_packet", data_hex=data.hex(), packet_hash=packet.packet_hash.hex())
destination.set_packet_callback(packet_callback)

rust_destination = None
rust_link = None

def link_packet_callback(message, packet):
    emit("python_link_packet", data_hex=message.hex(), context=packet.context)

def resource_concluded(resource):
    if resource.status == RNS.Resource.COMPLETE:
        resource.data.seek(0); data = resource.data.read()
        emit("python_resource_received", sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    else:
        emit("python_resource_failed", status=resource.status)

def resource_sent(resource):
    emit("python_resource_sent", status=resource.status)

def link_established(link):
    global rust_link
    rust_link = link
    link.set_packet_callback(link_packet_callback)
    link.set_resource_strategy(RNS.Link.ACCEPT_ALL)
    link.set_resource_concluded_callback(resource_concluded)
    emit("python_link_established", link_id=link.link_id.hex())
    RNS.Packet(link, b"python-to-rust over link").send()

def link_closed(link):
    emit("python_link_closed", link_id=link.link_id.hex(), status=link.status)

class RustAnnounceHandler:
    aspect_filter = "interop.rust"
    receive_path_responses = True
    def received_announce(self, destination_hash, announced_identity, app_data, announce_packet_hash, is_path_response):
        global rust_destination
        emit("rust_announce", dest_hash=destination_hash.hex(),
             app_data_hex=app_data.hex() if app_data is not None else None,
             announce_packet_hash=announce_packet_hash.hex(), is_path_response=bool(is_path_response))
        if not is_path_response:
            out = RNS.Destination(announced_identity, RNS.Destination.OUT, RNS.Destination.SINGLE, "interop", "rust")
            rust_destination = out
            RNS.Packet(out, b"python-to-rust via rust announce").send()
            emit("python_sent_packet_to_rust", dest_hash=destination_hash.hex())

RNS.Transport.register_announce_handler(RustAnnounceHandler())
emit("ready", port=port, python_dest_hash=destination.hash.hex())

signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
try:
    for line in sys.stdin:
        command = line.strip()
        if command == "announce_py":
            destination.announce()
            emit("python_announced", dest_hash=destination.hash.hex())
        elif command == "link_rust":
            if rust_destination is None:
                emit("python_link_error", reason="rust destination is unknown")
            else:
                rust_link = RNS.Link(rust_destination)
                rust_link.set_link_established_callback(link_established)
                rust_link.set_link_closed_callback(link_closed)
                emit("python_link_requested", link_id=rust_link.link_id.hex())
        elif command == "send_resource_rust":
            if rust_link is None:
                emit("python_resource_error", reason="link is unavailable")
            else:
                RNS.Resource(bytes((i % 251 for i in range(100000))), rust_link,
                             callback=resource_sent, auto_compress=False)
        elif command == "stop":
            break
except (KeyboardInterrupt, SystemExit):
    pass
"#;

fn run_tcp_gate(python: &str) -> i32 {
    let python_path = if python.is_empty() { "python3" } else { python };
    eprintln!("[gate] spawning Python RNS (interpreter: {})", python_path);
    let mut python = PythonRns::spawn(python_path);
    let ready = python.wait_for_event(TIMEOUT, |ev| ev["event"] == "ready");
    let port = ready["port"].as_u64().unwrap() as u16;
    let python_dest_hash = DestHash(parse_hex_16(ready["python_dest_hash"].as_str().unwrap()));
    eprintln!(
        "[gate] Python RNS up -> port {} dest {}",
        port,
        hex(&python_dest_hash.0)
    );

    let (tx, rx) = mpsc::channel();
    let node = start_rust_node(port, tx);

    let rust_identity = Identity::new(&mut OsRng);
    let rust_dest = Destination::single_in(
        APP_NAME,
        &[RUST_ASPECT],
        IdentityHash(*rust_identity.hash()),
    );
    node.register_destination(rust_dest.hash.0, rust_dest.dest_type.to_wire_constant())
        .expect("Rust destination registration");
    let rust_private = rust_identity.get_private_key().unwrap();
    let rust_public = rust_identity.get_public_key().unwrap();
    let mut sig_prv = [0u8; 32];
    let mut sig_pub = [0u8; 32];
    sig_prv.copy_from_slice(&rust_private[32..64]);
    sig_pub.copy_from_slice(&rust_public[32..64]);
    node.register_link_destination(rust_dest.hash.0, sig_prv, sig_pub, 1)
        .expect("Rust link destination registration");

    // --- Path discovery: Python announce, Rust learns path (hops == 1) ---
    eprintln!("[gate] (1) announce/path: Python announce -> Rust");
    let p_announce = request_python_announce(&mut python, &rx, python_dest_hash, TIMEOUT);
    assert_eq!(p_announce.hops, 1, "110");
    assert!(p_announce.public_key.iter().any(|b| *b != 0), "111");

    // --- Rust -> Python packet over path ---
    eprintln!("[gate] (2) Rust -> Python packet over path");
    let python_out = Destination::single_out(APP_NAME, &[PYTHON_ASPECT], &p_announce);
    node.send_packet(&python_out, RUST_TO_PYTHON_PAYLOAD)
        .expect("Rust send to Python destination");
    let py_pkt = python.wait_for_event(TIMEOUT, |ev| {
        ev["event"] == "python_packet" && ev["data_hex"] == hex(RUST_TO_PYTHON_PAYLOAD)
    });
    assert_eq!(decode_hex(py_pkt["data_hex"].as_str().unwrap()), RUST_TO_PYTHON_PAYLOAD, "120");

    // --- Rust announce; Python responds over the path ---
    eprintln!("[gate] (3) Rust announce + Python return packet");
    node.announce(&rust_dest, &rust_identity, Some(b"rust-appdata"))
        .expect("Rust announce");
    python.wait_for_event(TIMEOUT, |ev| {
        ev["event"] == "python_sent_packet_to_rust" && ev["dest_hash"] == hex(&rust_dest.hash.0)
    });
    let (raw, _ph) = wait_for_rust_delivery(&rx, rust_dest.hash, TIMEOUT);
    let plaintext = decrypt_delivery(&raw, &rust_identity);
    assert_eq!(plaintext, PYTHON_TO_RUST_PAYLOAD, "130");

    // --- Link: Python initiates, Rust serves ---
    eprintln!("[gate] (4) Link establish (Python initiated, Rust serves)");
    python.command("link_rust");
    let py_link = python.wait_for_event(TIMEOUT, |ev| ev["event"] == "python_link_established");
    let link_id = parse_hex_16(py_link["link_id"].as_str().unwrap());
    let rust_link = wait_for_rust_event(&rx, TIMEOUT, |ev| match ev {
        RustEvent::LinkEstablished { link_id, is_initiator } => Some((*link_id, *is_initiator)),
        _ => None,
    })
    .expect("Rust responder should establish Python-initiated link");
    assert_eq!(rust_link, (link_id, false), "140");

    // --- Link data both ways ---
    let py_link_data = wait_for_rust_event(&rx, TIMEOUT, |ev| match ev {
        RustEvent::LinkData { link_id: id, context, data } if *id == link_id => Some((*context, data.clone())),
        _ => None,
    })
    .expect("Rust should receive Python link data");
    assert_eq!(py_link_data, (0, b"python-to-rust over link".to_vec()), "150");

    node.send_on_link(link_id, b"rust-to-python over link".to_vec(), 0)
        .expect("Rust send over link");
    let r_link_data = python.wait_for_event(TIMEOUT, |ev| {
        ev["event"] == "python_link_packet" && ev["context"] == 0
            && ev["data_hex"] == hex(b"rust-to-python over link")
    });
    assert_eq!(decode_hex(r_link_data["data_hex"].as_str().unwrap()), b"rust-to-python over link", "160");

    // --- Resource RX: Python -> Rust (100000 bytes) ---
    eprintln!("[gate] (5) Resource RX: Python -> Rust (100000 B)");
    python.command("send_resource_rust");
    let py_resource = wait_for_rust_event(&rx, TIMEOUT, |ev| match ev {
        RustEvent::ResourceReceived { link_id: id, data } if *id == link_id => Some(data.clone()),
        _ => None,
    })
    .expect("Rust should receive Python 100000-byte Resource");
    assert_eq!(py_resource.len(), 100000, "170");
    assert!(
        py_resource.iter().enumerate().all(|(i, b)| *b == (i % 251) as u8),
        "171: chunk reassembly content mismatch"
    );

    // --- Resource TX: Rust -> Python (100000 bytes), verify digest + proof ---
    eprintln!("[gate] (6) Resource TX: Rust -> Python (100000 B)");
    let rust_resource: Vec<u8> = (0..100000).map(|i| (i % 239) as u8).collect();
    node.send_resource(link_id, rust_resource.clone(), None)
        .expect("Rust send Resource to Python");
    wait_for_rust_event(&rx, TIMEOUT, |ev| match ev {
        RustEvent::ResourceCompleted { link_id: id } if *id == link_id => Some(()),
        _ => None,
    })
    .expect("Rust should receive proof for its Resource");
    let py_received = python.wait_for_event(TIMEOUT, |ev| {
        ev["event"] == "python_resource_received" && ev["size"] == 100000
    });
    assert_eq!(
        py_received["sha256"],
        hex(&rns_core::hash::full_hash(&rust_resource)),
        "180: Python reassembly digest must equal sender hash"
    );

    node.shutdown();
    eprintln!("\n[gate] T0 GATE: PASS — Link + Resource interop with Python RNS verified bidirectionally");
    0
}

fn main() {
    env_logger::init();
    let args: Vec<String> = std::env::args().collect();
    if args.iter().any(|a| a == "--rnode") {
        eprintln!("[gate] --rnode mode: radio interface requires connected hardware.");
        eprintln!("[gate] NOT RUN: no RNode serial detected on this host (design T0 RF-live leg).");
        std::process::exit(2);
    }
    let python = std::env::var("LMAO_PYTHON").unwrap_or_default();
    match std::panic::catch_unwind(|| run_tcp_gate(&python)) {
        Ok(0) => std::process::exit(0),
        Ok(code) => std::process::exit(code),
        Err(_) => {
            eprintln!("\n[gate] T0 GATE: FAIL — Link/Resource interop assertion failed (see above)");
            std::process::exit(1);
        }
    }
}
