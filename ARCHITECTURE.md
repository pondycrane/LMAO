# LMAO — Leave Me Alone Offgrid

## System Architecture (Reticulum/LXMF)

### Stack

```
Application   │  Chat · IoT Ingest · Command Dispatch · Call Signaling
              │  K8s Pods (gRPC Clients)
──────────────┼────────────────────────────────────────────────────────
gRPC API      │  Send · Subscribe · GetIdentity
              │  protobuf service on port 50051
──────────────┼────────────────────────────────────────────────────────
LXMF          │  Message format: Dest|Src|Sig|Payload[Timestamp,Content,Title,Fields]
              │  Propagation Nodes (store-and-forward) · LXM Router (retries/ACKs)
──────────────┼────────────────────────────────────────────────────────
Reticulum     │  X25519+Ed25519 identities · 16-byte destination hashes
              │  Links (bidirectional, forward secrecy) · Opportunistic packets
              │  Resources (reliable file xfer) · Transport-agnostic multi-hop routing
──────────────┼────────────────────────────────────────────────────────
Interfaces    │  LoRa (RNode) · WiFi (AutoInterface) · TCP/UDP · Serial
```

## Device Classes

| Device | Hardware | Link | Role |
|--------|----------|------|------|
| **LMAO IoT endpoint** | ESP32-S3 + LoRa + sensor | LoRa only | Sleep → read → send LXMF → brief command poll → sleep |
| **LMAO Camera node** | ESP32-S3-CAM + LoRa/WiFi | LoRa or WiFi | Same, but captures WebP image on command |
| **RNode** | ESP32/RP2040 + LoRa radio | LoRa ↔ USB/WiFi | Transparent LoRa bridge |
| **LMAO Server** | RPi/NUC | LoRa + WiFi + TCP | Propagation Node · IoT processor · Command scheduler · Human services · **gRPC API (port 50051)** · **Docker container** |
| **K8s Pod** | In-cluster container | TCP (gRPC) | Automated clients: IoT ingest, command dispatch, monitoring — reach server via K8s Service |
| **LMAO Human Client** | Laptop/Desktop (Python CLI) | WiFi + optional LoRa (RNode) | First-party terminal client for human messaging via LMAO protobuf protocol |
| **Human nodes** | Phone (Sideband) / Laptop (NomadNet/MeshChat) | WiFi (preferred) | Person-to-person: text · images · audio clips · calls |
| **Backbone** | Ubiquiti/MikroTik radios | Long-range WiFi | High-capacity between sites |

## Topology

```
  Sensors ──LoRa──→ RNode ──USB/WiFi──→ Central Server ←──WiFi mesh── Humans
  Actuators ←──LoRa── RNode          │   (RPi/NUC)                 ↕
                              K8s Pods (gRPC)           Propagation Node
                         IoT Ingest · Cmd Dispatch      (store-and-forward)
                         Monitoring · Automation
```

- **LoRa leaf**: every device hears every packet; server moderates airtime
- **WiFi mesh**: human communication, image/audio transfers, call streams
- **K8s pods**: automated clients communicate via gRPC on port 50051; the server bridges gRPC ↔ LXMF
- **Server bridges all worlds**: translates between high- and low-speed networks

## IoT Data Flow

1. Sensor wakes, reads peripherals, builds LXMF message addressed to server's 16-byte hash
2. Sends over LoRa (or WiFi if available)
3. Listens 2-15 s for incoming commands, then deep sleeps
4. Server receives → parses → stores (SQLite/InfluxDB) → evaluates rules → sends commands if triggered
5. Propagated commands wait on the server's Propagation Node until the target node wakes and collects

## Messaging per Content Type

| Type | LXMF Mechanism | Bandwidth Needs |
|------|----------------|-----------------|
| **Text** | Single LXMF packet (opportunistic) | Fits LoRa (~400 B) |
| **Sensor reading** | LXMF packet with Fields/Content | ~12-70 B → fits LoRa |
| **Command** | LXMF packet, server retries until ACK | ~10-50 B → fits LoRa |
| **Voice clip / Image** | LXMF Resource (reliable transfer) | KB→MB → WiFi only |
| **Real-time call** | Raw Reticulum Link (not LXMF) — stream Opus/H.264 frames | ~30-100 kbps → WiFi only |
| **Location** | LXMF packet (opportunistic) | ~16 B → fits LoRa |

## Human Client

The `human_client/` package provides a first-party terminal CLI for human
operators on laptops/desktops. It communicates using the same protobuf
`LMAOEnvelope → TextMessage` protocol as the server, making it a drop-in
replacement for third-party apps (Sideband, NomadNet).

### Features

- WiFi AutoInterface always enabled (works without LoRa hardware)
- Optional RNode LoRa interface for mesh connectivity
- Interactive REPL with `/send`, `/dest`, `/help`, `/quit` commands
- Protobuf TextMessage encode/decode with raw UTF-8 fallback
- Incoming message display with source hash
- No auto-ACK (human decides whether to respond)

### Key Targets

| Target | Description |
|--------|-------------|
| `//human_client:client` | CLI binary — `bazel run //human_client:client` |
| `//human_client:client_lib` | Shared library for tests |
| `//tests:test_human_client` | Unit tests (mocked RNS/LXMF) |

### gRPC API

A parallel API surface (alongside LXMF) for automated K8s pod clients.
Defined in [`proto/lma.proto`](../proto/lma.proto) and served on port 50051.

| RPC | Type | Purpose |
|-----|------|---------|
| `Send` | Unary | Inject protobuf envelope into LXMF mesh |
| `Subscribe` | Server-streaming | Stream incoming LXMF messages (with optional title_filter) |
| `GetIdentity` | Unary | Return server Reticulum identity hex |

*The `Tunnel` RPC is commented out in the .proto — planned for v0.2.*

See [`README.md`](../README.md#7-grpc-api-k8s-pod-integration) for usage examples.

### Usage

```bash
# Start the client (WiFi-only, no RNode needed)
bazel run //human_client:client

# With a specific RNode port
LMAO_RNODE_PORT=/dev/ttyACM0 bazel run //human_client:client
```

### Message Flow

#### Sending

1. Client types message → protobuf LMAOEnvelope → LXMF → RNS → WiFi/LoRa
2. Server receives → handler parses → sends ACK → client displays ACK

#### Receiving

3. Client displays incoming messages with `>>> MSG from <hex_hash>: <content>`

The client does **not** send automatic ACK replies — the human operator
chooses whether to respond. This prevents message loops.

## Protobuf Recommendation

### Where it lives

The canonical protobuf schema lives at `proto/lma.proto` (was `lmao_server/proto/lma.proto`).
Generated stubs are produced by Bazel at build time and are **not** checked in.
Python code imports via the `lma_core` wrapper:

```python
from lma_core import LMAOEnvelope, TextMessage
```

Replace the msgpack `Fields` dict with a **protobuf blob inside Content**:

```
LXMF envelope: [Dest|Src|Sig | Timestamp | <protobuf bytes> | Title="p:Envelope"]
```

A single discriminator byte or string in `Title` tells the receiver to decode via protobuf.

### Why

| Data | msgpack Fields | Protobuf | Saving |
|------|---------------|----------|--------|
| Temp + humidity (2 readings) | ~71 B | ~12 B | **5.9×** |
| GPS location | ~62 B | ~16 B | **3.9×** |
| Command "spray 60s" | ~48 B | ~10 B | **4.8×** |

On LoRa with ~200 B/packet budget after encryption, this is the difference between 1 reading per packet and 6-8.

### Schema structure (one `.proto` file for everything)

```protobuf
syntax = "proto3";
package lma;

message LMAOEnvelope {
  oneof payload {
    SensorReport    sensor  = 10;
    CommandRequest  command = 11;
    CommandAck      ack     = 12;
    TextMessage     text    = 20;
    AudioMessage    audio   = 21;
    ImageMessage    image   = 22;
    CallSignal      call    = 30;
  }
}

message SensorReport {
  string node_id = 1;
  uint32 seq     = 2;
  float  battery = 3;
  repeated SensorReading readings = 4;  // 7 B each: tag(1B)+varint(1B)+tag(1B)+float(4B)
}
message CommandRequest {
  string cmd_id     = 1;
  string target     = 2;
  string action     = 3;  // "spray", "open_valve", "reboot"
  map<string,string> params = 4;
  uint64 issued_ms  = 5;
  uint64 expires_ms = 6;
}
message CallSignal {
  enum Signal { OFFER=0; ANSWER=1; ICE=2; HANGUP=3; KEEPALIVE=4; }
  Signal signal        = 1;
  string sdp_or_ice    = 2;
  string media_type    = 3;
}
// ... TextMessage, AudioMessage, ImageMessage, CommandAck follow similar patterns
```

### Build System Integration (Bazel)

The project uses [Bazel](https://bazel.build/) (v7.4.1) for hermetic builds and
proto code generation. See `.bazelversion` and `MODULE.bazel`.

#### Key Targets

| Target | Description |
|--------|-------------|
| `//proto:lma_proto` | Raw proto library (language-agnostic) |
| `//proto:lma_py_proto` | Python generated protobuf stubs |
| `//lma_core` | Shared Python wrapper re-exporting proto stubs |
| `//lmao_server` | Server binary (RNode + LXMF) |
| `//human_client:client` | Human CLI client (WiFi + optional RNode) |
| `//tests:test_lma_encoder` | Encoder compatibility tests |
| `//tests:test_server_handler` | Server handler tests |
| `//tests:test_human_client` | Human client tests |

#### Proto Schema

The canonical protobuf schema lives at `proto/lma.proto` (was `lmao_server/proto/lma.proto`).
Generated stubs (`lma_pb2.py`) are produced by Bazel at build time and are **not** checked in.

#### Common Commands

```bash
# Build everything
bazel build //proto:all //lma_core //lmao_server //tests:all

# Run tests
bazel test //tests:all

# Generate protobuf stubs explicitly
bazel build //proto:lma_py_proto
```

Multi-language stubs (Go, Kotlin, nanopb) are planned but not yet wired — see
the placeholder comments in `proto/BUILD`.

#### Vendored urns Library

The Cardputer client bundles a vendored MicroPython port of µReticulum ("urns")
at `cardputer_client/lib/urns/`.  This is the full µReticulum stack — identity,
packet routing, LXMF, crypto (Ed25519, X25519, AES, hashes) — ported to
MicroPython.  Native `.mpy` modules (`lib/ed25519_fast_xtensawin.mpy`,
`lib/bz2_fast_xtensawin.mpy`) provide hardware-accelerated crypto on the
Xtensa (ESP32-S3) architecture.

Library files are auto-discovered by the flash tool (`os.walk()`) and uploaded
to the Cardputer under `/lib/`.  No manual list updates are needed when new
library files are added.

### Per-platform codegen

| Platform | Tool | Notes |
|----------|------|-------|
| **LMAO Server (Python)** | Bazel + `py_proto_library` | Full `protobuf` library, stubs generated at build time |
| **Cardputer (urns)** | Vendored MicroPython port in `cardputer_client/lib/urns/` | µReticulum MicroPython port; uploaded to device at flash time |
| **LMAO IoT (µReticulum)** | **nanopb** compiled as native `.mpy` | <10 KB RAM, static buffers, no malloc |
| **LMAO IoT (alt)** | Hand-written minimal encoder in MicroPython | Only encodes SensorReport, decodes CommandRequest |
| **Android (optional fork)** | `protoc --kotlin_out=` | If you build a custom Sideband |

### Key benefits

- **~4-6× smaller payloads** on LoRa where bytes are the scarcest resource
- **Single `.proto` file = API contract** across Python, C, Kotlin, with no ambiguity
- **`oneof` dispatch** means receiver knows the message type in 1 byte
- **Backward compatible** — new fields don't break old nodes (protobuf preserves unknown fields)
- **Cross-platform out of the box** — `protoc` generates stubs for every language you'll touch

## Post-Simplification (2026-07-08)

A structural refactor eliminated duplicated code, dead imports, and
layering violations across the host-side codebase.  No behaviour was
changed — every change is revertible in a single `git revert`.

### New / changed modules

| Module | Change |
|--------|--------|
| `lma_core/message_utils.py` | **New** — shared `decode_lmao_message()` extracted from server + client handlers (~150 lines deduplicated) |
| `lma_core/rns_di.py` | **New** — DI wrapper for RNS/LXMF; tests monkeypatch attributes instead of `sys.modules` |
| `lma_core/config_utils.py` | Added `RnsConfig` factory (was only free functions) |
| `lma_core/__init__.py` | Now exports `add_LMAOServicer_to_server`; removed `TunnelRequest`/`TunnelResponse` |
| `lmao_server/config.py` | Reduced from ~75 to ~20 lines (uses `RnsConfig`) |
| `human_client/config.py` | Same reduction |
| `lmao_server/server.py` | Removed Tunnel handler; import path changes |
| `human_client/client.py` | Removed unused `DecodeError` import |
| `k8s-app/iot_ingest.py` | Imports proto stubs from `lma_core` instead of `proto.*` directly |
| `cardputer_client/proto/lma_encoder.py` | Extracted `_decode_proto_message()` generic helper; 5 decode functions simplified |
| `cardputer_client/main.py` | Removed `_print_exception` CPython shim |
| `proto/lma.proto` | Commented out `Tunnel` RPC (TODO v0.2) |

### Test infrastructure

| File | Change |
|------|--------|
| `tests/conftest.py` | **New** — shared `setup_common_mocks()` / `cleanup_common_mocks()` (~75 lines deduplicated from 2 files) |
| `tests/test_server_handler.py` | Split into `test_server_handler.py` (353 lines), `test_server_grpc.py` (274 lines), `test_server_startup.py` (405 lines) |
| `tests/test_human_client.py` | Split into `test_human_client.py` (397 lines), `test_client_repl.py` (314 lines), `test_client_startup.py` (223 lines) |

### Line-count impact

| Before | After | Δ |
|--------|-------|---|
| 2 config modules, ~150 lines total | 2 config modules, ~40 lines total | −110 |
| 2 handler decode blocks, ~150 lines total | 0 (shared `message_utils.py`) | −150 |
| 2 mock-setup blocks, ~150 lines total | 1 (`conftest.py`) | −70 |
| Largest test file: 991 lines | Largest test file: 405 lines | −59% |
| `Tunnel` RPC placeholder | Removed | −30 lines |
| `_print_exception` shim | Removed | −10 lines |

### Deferred

- **Task 10 (Split Transport god object):** The 1,487-line `cardputer_client/lib/urns/transport.py`
  was not split because the refactor requires Cardputer hardware testing to validate
  MicroPython compatibility, memory overhead, and import behaviour. This should be
  done as part of a dedicated hardware testing session.