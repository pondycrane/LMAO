# LMAO — LoRa Mesh Communication POC

**Proof of Concept**: Bidirectional LoRa communication between a Raspberry Pi
server and an M5Stack Cardputer ADV client using the
[Reticulum](https://reticulum.network/) networking stack with
[LXMF](https://github.com/markqvist/LXMF) messaging.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                              LoRa RF (868/915 MHz)                           │
│  ┌───────────────┐  ┌──────────────────┐         ┌──────────────────────┐   │
│  │  Laptop/Desktop│  │  Raspberry Pi    │         │  M5Stack Cardputer   │   │
│  │  Human Client  │  │  ┌────────────┐  │         │  ADV                 │   │
│  │  (Python CLI)  │  │  │ LMAO Server│  │         │  ┌────────────────┐  │   │
│  │  WiFi/AutoIFace│──┤  │RNS+LXMF+   │──┤◄──LoRa─┼──┤ µReticulum     │  │   │
│  │                │  │  │ gRPC API   │  │         │  │ client         │  │   │
│  └────────────────┘  │  └─────┬──────┘  │         │  └──────┬─────────┘  │   │
│                      │        │ USB      │         │         │ SPI         │   │
│  ┌──────────────┐    │  ┌─────┴──────┐  │         │  ┌──────┴─────────┐  │   │
│  │ Docker       │    │  │ ESP32 RNode│  │         │  │ SX1262 LoRa    │  │   │
│  │ Container    │────┤  │ (LoRa br.) │──┼────LoRa─┼──│ radio + ant    │  │   │
│  │ (gRPC client)│    │  └────────────┘  │         │  └────────────────┘  │   │
│  └──────────────┘    └──────────────────┘         └──────────────────────┘   │
│                                │ gRPC :50051                                 │
│                    ┌───────────┴────────────┐                                │
│                    │  K8s Cluster           │                                │
│                    │  ┌───────────────────┐ │                                │
│                    │  │ IoT Ingest Pod    │ │                                │
│                    │  │ (gRPC client)     │ │                                │
│                    │  └───────────────────┘ │                                │
│                    │  ┌───────────────────┐ │                                │
│                    │  │ Command Dispatch  │ │                                │
│                    │  │ (gRPC client)     │ │                                │
│                    │  └───────────────────┘ │                                │
│                    └────────────────────────┘                                │
└──────────────────────────────────────────────────────────────────────────────┘
```

## Quickstart

### Prerequisites

| Component | Requirements |
|-----------|-------------|
| Raspberry Pi | Python 3.8+, USB port |
| ESP32 RNode | Flashed with RNode firmware |
| Cardputer ADV | M5Stack Cardputer with LoRa antenna, MicroPython installed |
| Laptop/Desktop | Python 3.8+, optional RNode USB for LoRa |
| LoRa band | Matching frequency (868 MHz EU / 915 MHz US) |
| Bazel | v7.4.1 (see `.bazelversion`) — use [bazelisk](https://github.com/bazelbuild/bazelisk) (auto-selects correct version via `.bazelversion`). Install: `npm install -g @bazel/bazelisk` ([other install methods](https://github.com/bazelbuild/bazelisk#installation)). Ensure `~/.npm-global/bin` (or your npm global bin dir) is in `PATH`. Verify with `bazel --version` (expected: `bazel 7.4.1`). |
| Docker | For containerized deployment (optional) — `docker --version` |
| kubectl | For K8s Service deployment (optional) — `kubectl version --client` |

### 1. Flash the ESP32 RNode

Follow the guide in [`rnode_firmware/README.md`](rnode_firmware/README.md).

After flashing, verify:

```bash
rnodeconf --port /dev/ttyUSB0 --info
```

### 2. Build & Install Server Dependencies

The canonical build system is [Bazel](https://bazel.build/) (see `.bazelversion` for the
required version). Bazel generates protobuf stubs, resolves Python dependencies, and runs tests.

**Option A — Bazel (recommended):**

```bash
# Build everything (generates protobuf stubs, installs deps)
bazel build //lmao_server:server

# Run the server
bazel run //lmao_server:server
```

**Option B — pip (no Bazel):**

If you prefer to run without Bazel, you must first generate the protobuf stubs manually,
then install dependencies with pip:

```bash
# Generate protobuf Python stubs (required by lma_core)
protoc --python_out=. proto/lma.proto
# The generated file will be at proto/lma_pb2.py

# Install Python dependencies
cd lmao_server && pip3 install -r requirements.txt

# Run from repo root with PYTHONPATH including both lmao_server/ (for config) and repo root
cd .. && PYTHONPATH="$PWD/lmao_server:$PWD" python3 lmao_server/server.py
```

### 3. Configure the Server

The RNode port is auto-detected from common ports (`/dev/ttyUSB0`, `/dev/ttyACM0`, etc.).
Override with the `LMAO_RNODE_PORT` environment variable:

```bash
# Auto-detect (default)
python3 server.py

# Or specify the port explicitly
LMAO_RNODE_PORT=/dev/ttyACM0 python3 server.py
```

If no RNode is connected, the server starts in WiFi-only mode with a warning.

Edit `lmao_server/config.py` to adjust radio parameters:
- Set `frequency` for your region (868 MHz EU / 915 MHz US)
- Set `spreadingfactor`, `bandwidth`, `txpower` — **must match the client**

### 4. Start the Server

```bash
# Using Bazel (recommended)
bazel run //lmao_server:server

# Or without Bazel (from repo root, with PYTHONPATH including lmao_server/)
PYTHONPATH="$PWD/lmao_server:$PWD" python3 lmao_server/server.py
```

Expected output (same for both methods):

```
Initializing Reticulum...
Reticulum initialized.
Starting LXMF router...

==================================================
LMAO Server POC — Running
Node identity: 1a2b3c4d5e6f...
Listening for LXMF messages...
  LoRa: RNode on /dev/ttyUSB0
  Title discriminator: p:Envelope
==================================================
```

### 5. Configure and Flash the Cardputer

**Before flashing**, edit `cardputer_client/config.py`:
- Set `WIFI_SSID` and `WIFI_PASS` to match your local network (required for UDP interface)
- Optionally set `DEST_HASH` to the server's Reticulum identity hex (32 hex chars).
  Leave as `None` (default) to skip sending. The E2E test injects this
  automatically — you only need to set it for manual testing without the
  automated flash+test workflow. Obtain the server identity from its startup
  log (`Node identity: ...`).
- Optionally adjust `NODE_NAME` and `DEBUG` level

**Option A — MicroPython + cardputer_client** (lighter weight, requires setup):

**Using Bazel (recommended):**

```bash
# Auto-detect Cardputer serial port and flash
bazel run //cardputer_client:flash

# Specify port explicitly
bazel run //cardputer_client:flash -- --port /dev/ttyACM0

# Verify connection without flashing
bazel run //cardputer_client:flash -- --verify-only
```

**Or manually with ampy** (if you don't have Bazel):

```bash
# Upload client files
ampy --port /dev/ttyUSB1 put cardputer_client/config.py
ampy --port /dev/ttyUSB1 put cardputer_client/lora_boards.py
ampy --port /dev/ttyUSB1 put cardputer_client/main.py main.py
ampy --port /dev/ttyUSB1 put cardputer_client/proto/lma_encoder.py proto/lma_encoder.py

# Upload µReticulum library (urns port) to /lib/
for f in $(find cardputer_client/lib -name '*.py' -o -name '*.mpy'); do
  ampy --port /dev/ttyUSB1 put "$f" "${f#cardputer_client/}"
done
```
> Pinout presets for different LoRa boards are defined in `cardputer_client/lora_boards.py`.
> Add new presets there and reference them from `config.py` via the `board` key.

The Cardputer will auto-run `main.py` on boot and display:

```
LMAO POC Ready
ID: a1b2c3d4...
```

**Option B — RNode LoRa bridge** (heavier, if you have an RNode):

If you're using an external RNode LoRa radio instead of the Cardputer's
onboard SX1262, connect it via USB and configure the serial interface in
``config.py``. The RNode will appear as a standard serial port and handles
LoRa modulation independently.

> For alternative client firmware options (e.g., rsCardputer), see
> [docs/alternative-firmware.md](docs/alternative-firmware.md).

### 6. Test Communication

An automated E2E test can verify the full LoRa communication path with
both devices connected:

```bash
bazel test //tests:test_cardputer_lora_e2e --test_output=all
```

The test auto-skips when hardware is not detected.  See
[Section 10](#10-run-tests) for all test targets.

Manual verification steps:

1. Both devices powered on and within LoRa range
2. Cardputer sends "Hello from Cardputer — seq 1" every 10 seconds
3. Server displays: `MSG from <hash>: Hello from Cardputer`
4. Server replies: `ACK from LMAO Server — received your message`
5. Cardputer displays the reply on screen

### 7. gRPC API (K8s Pod Integration)

The server exposes a gRPC API on port `50051` for K8s pods and other
automated clients to interact with the LoRa mesh programmatically.

**Proto definition**: [`proto/lma.proto`](proto/lma.proto)

| RPC | Type | Description |
|-----|------|-------------|
| `Send` | Unary | Inject a protobuf `LMAOEnvelope` into the LXMF mesh addressed to `destination_hash` |
| `Subscribe` | Server-streaming | Stream incoming LXMF messages to the client; optional `title_filter` |
| `Tunnel` | Bidirectional-streaming | Bidirectional raw LXMF packet tunnel (not yet implemented) |
| `GetIdentity` | Unary | Return the server's Reticulum identity hex and node name |

**Example** (Python):

```python
import grpc
from proto import lma_pb2, lma_pb2_grpc

channel = grpc.insecure_channel("localhost:50051")
stub = lma_pb2_grpc.LMAOStub(channel)

# Send a message
stub.Send(lma_pb2.SendRequest(
    envelope=envelope_bytes,
    destination_hash="a1b2c3d4..."
))

# Subscribe to incoming messages
for msg in stub.Subscribe(lma_pb2.SubscribeRequest(title_filter="p:Envelope")):
    print(f"Received {len(msg.envelope)} bytes from {msg.source_hash}")

# Get server identity
identity = stub.GetIdentity(lma_pb2.GetIdentityRequest())
print(f"Server: {identity.identity_hex}")
```

See [`k8s-app/iot_ingest.py`](k8s-app/iot_ingest.py) for a complete example.

### 8. Docker Deployment

A Docker image is available for containerized deployment of the server
(on the Raspberry Pi or any Linux host with an RNode).

```bash
# Build the image
docker build -t lmao-server .

# Run (requires --network host for Reticulum and RNode USB passthrough)
docker run --network host --device /dev/ttyUSB0:/dev/ttyUSB0 lmao-server
```

**Important**:
- `--network host` is **required** — Reticulum uses UDP multicast for
  AutoInterface discovery and must run on the host network stack.
- Pass your RNode device with `--device` (adjust path as needed).
- Set `LMAO_RNODE_PORT` to override the auto-detected port:
  ```bash
  docker run --network host --device /dev/ttyACM0:/dev/ttyACM0 \
    -e LMAO_RNODE_PORT=/dev/ttyACM0 lmao-server
  ```
- The gRPC API on port 50051 is accessible on the host.

### 9. Kubernetes Deployment

Pods in a K8s cluster can reach the external LMAO server (running on a
physical Raspberry Pi) via a headless Service with manually managed Endpoints.

```bash
# 1. Edit the RPi IP in k8s/lmao-service.yaml (default: 192.168.1.100)
# 2. Apply the manifest
kubectl apply -f k8s/lmao-service.yaml

# 3. Pods connect via the stable DNS name
#    lmao-server.default.svc.cluster.local:50051
```

The example K8s app at [`k8s-app/iot_ingest.py`](k8s-app/iot_ingest.py)
can be used from any pod to interact with the server:

```bash
# Set LMAO_SERVER env var (defaults to localhost:50051 for local dev)
export LMAO_SERVER=lmao-server.default.svc.cluster.local:50051
python k8s-app/iot_ingest.py --send --get-identity
```

### 10. Run Tests

```bash
# Run all unit tests (no hardware required)
bazel test //tests:all

# Run a specific unit test
bazel test //tests:test_lma_encoder --test_output=all

# Run the E2E flash test (requires physical Cardputer hardware)
bazel test //tests:test_cardputer_e2e --test_output=all

# Run the LoRa E2E test (requires Cardputer + Heltec RNode)
bazel test //tests:test_cardputer_lora_e2e --test_output=all
```

The E2E tests auto-skip when the required hardware is not detected.

### 11. Run the Human Client

```bash
# Using Bazel (recommended)
bazel run //human_client:client

# Or without Bazel (from repo root)
PYTHONPATH="$PWD" python3 human_client/client.py

# With a specific RNode port
LMAO_RNODE_PORT=/dev/ttyACM0 bazel run //human_client:client
```

The Human Client starts with WiFi AutoInterface (no RNode required).
If an RNode is connected, LoRa messaging is available.

---

## Project Structure

```
├── README.md                          # This file
├── ARCHITECTURE.md                    # Full system architecture reference
├── AGENTS.md                          # Project rules (E2E flash verification)
├── Dockerfile                         # Container build for server deployment
├── .bazelversion                      # Bazel version pin (7.4.1)
├── MODULE.bazel                       # Bazel module definition
│
├── proto/                             # Canonical protobuf schema (single source of truth)
│                                     # (moved from lmao_server/proto/ — now generated by Bazel)
│   ├── BUILD                          # Bazel: proto_library + py_proto_library targets
│   ├── lma.proto                      # Protobuf schema (all message types + gRPC service)
│   ├── __init__.py                    # Package marker
│   ├── lma_pb2.py                     # Generated protobuf Python stubs
│   └── lma_pb2_grpc.py                # Generated gRPC Python stubs
│
├── lma_core/                          # Shared Python wrapper library
│   ├── BUILD                          # Bazel: py_library target
│   ├── __init__.py                    # Re-exports generated protobuf stubs
│   ├── config_utils.py                # RNode port resolution + INI generation helpers
│   ├── message_utils.py               # Shared LXMF message decoding (decode_lmao_message)
│   └── rns_di.py                      # RNS/LXMF dependency-injection wrapper for testability
│
├── lmao_server/                       # Python — runs on Raspberry Pi
│   ├── BUILD                          # Bazel: py_binary target
│   ├── __init__.py                    # Package marker
│   ├── requirements.txt               # Python dependencies (rns, lxmf, protobuf, grpcio)
│   ├── requirements_lock.txt          # Pinned pip dependencies for Bazel
│   ├── config.py                      # Reticulum config with RNode LoRa interface
│   └── server.py                      # Main server: RNS + LXMF router + gRPC API
│
├── human_client/                      # Python — runs on laptop/desktop
│   ├── BUILD                          # Bazel: py_binary + py_library targets
│   ├── __init__.py                    # Package marker
│   ├── config.py                      # Reticulum config (WiFi + optional RNode)
│   └── client.py                      # Interactive REPL for human messaging
│
├── k8s/                               # Kubernetes manifests
│   └── lmao-service.yaml              # Headless Service + Endpoints for external RPi
│
├── k8s-app/                           # Example K8s pod application
│   └── iot_ingest.py                  # gRPC client: Send + Subscribe + GetIdentity
│
├── cardputer_client/                  # MicroPython — runs on M5Stack Cardputer
│   ├── boot.py                        # MicroPython boot script (sets /lib in path)
│   ├── config.py                      # µReticulum config for onboard LoRa
│   ├── main.py                        # Client: periodic hello + reply display
│   ├── lib/                           # Vendored µReticulum library (urns port)
│   └── proto/
│       ├── BUILD                      # Bazel: py_library for host-side tests
│       ├── lma.proto                  # Same protobuf schema (reference)
│       └── lma_encoder.py             # Hand-coded minimal encoder (no protobuf dep)
│
├── tests/                             # Host-side tests (Bazel py_test targets)
│   ├── BUILD                          # Bazel: py_test targets
│   ├── conftest.py                    # Shared mock helpers (setup_common_mocks / cleanup_common_mocks)
│   ├── test_config.py                 # Config module unit tests (no hardware)
│   ├── test_lma_core.py               # lma_core import error handling + exports
│   ├── test_lma_encoder.py            # Encoder round-trip + cross-validation tests
│   ├── test_server_handler.py         # Server handler unit tests (mocked RNS/LXMF)
│   ├── test_server_startup.py         # Server startup lifecycle + async entry point tests
│   ├── test_client_repl.py            # Human client REPL input parsing tests
│   ├── test_client_startup.py         # Human client startup lifecycle tests
│   └── e2e/
│       └── test_cardputer_flash.py    # E2E flash + boot validation test
│
└── rnode_firmware/                    # Documentation only
    └── README.md                      # Step-by-step ESP32 RNode flashing guide
```

---

## Message Protocol

Messages are [LXMF](https://github.com/markqvist/LXMF) packets with:

| Field | Value |
|-------|-------|
| **Title** | `p:Envelope` (protobuf discriminator) |
| **Content** | Protobuf-encoded `LMAOEnvelope` bytes |
| **Method** | Opportunistic (single-packet, best-effort) |

The protobuf schema supports multiple message types for different use cases.
See [`proto/lma.proto`](proto/lma.proto) for the complete definitions.

| Message Type | Field ID | Purpose | Wire Size (typical) |
|-------------|----------|---------|---------------------|
| `TextMessage` | 20 | Human-to-human text (node_id, content, timestamp) | ~45 B |
| `SensorReport` | 10 | IoT sensor readings (node_id, seq, battery, readings[]) | ~30-150 B |
| `CommandRequest` | 11 | Server-to-node commands (cmd_id, target, action, params) | ~50-200 B |
| `CommandAck` | 12 | Node command acknowledgements (cmd_id, node_id, success, msg) | ~40 B |
| `AudioMessage` | 21 | Voice clips (node_id, audio_data, codec, duration_ms) | varies (WiFi) |
| `ImageMessage` | 22 | Image transfers (node_id, image_data, format, width, height) | varies (WiFi) |
| `CallSignal` | 30 | WebRTC call signaling (OFFER/ANSWER/ICE/HANGUP/KEEPALIVE) | ~100-500 B |

> **Note:** Audio, image, and call signal payloads typically exceed LoRa's ~200 B
> per-packet limit and are better suited for WiFi or other high-bandwidth
> interfaces. Text, sensor, and command messages fit comfortably in LoRa packets.

---

## Scope (POC Only)

This POC intentionally limits scope to:

- ✅ Direct LoRa communication (single-hop, no propagation)
- ✅ Text messages between Cardputer, RPi server, and Human Client (Python CLI)
- ✅ LXMF acknowledgements
- ✅ Protobuf-encoded payloads
- ✅ gRPC API for K8s pod integration (Send, Subscribe, GetIdentity)
- ✅ Docker containerization
- ✅ K8s Service + Endpoints for external RPi discovery
- ❌ No multi-hop / store-and-forward
- ✅ WiFi fallback (AutoInterface enabled when RNode is not connected)
- ❌ No sensor integration in server
- ❌ No image/audio/file transfer
- ❌ No encryption key management
- ❌ No battery optimization

For the full system design, see [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## Troubleshooting

| Problem | Check |
|---------|-------|
| Server can't find RNode | Is ESP32 plugged in? Set `LMAO_RNODE_PORT` or check auto-detected port |
| Server hangs with no output | RNode port not found — the server now warns and starts in WiFi-only mode. Check `LMAO_RNODE_PORT`. |
| No LoRa packets despite devices on same frequency | Check **all** radio parameters match: SF, BW, CR, and TXP (not just frequency) |
| Cardputer has µReticulum firmware, not MicroPython | That's expected with rsCardputer firmware — it's a valid LXMF client. Use Option B above. |
| No LoRa packets | Both devices on same frequency? In range? |
| Cardputer display blank | ST7789 driver installed? SPI pins correct? |
| "Permission denied" on serial | `sudo usermod -a -G dialout $USER` |
| Protobuf import error | Bazel: run `bazel build //proto:lma_py_proto`. Without Bazel: run `protoc --python_out=. proto/lma.proto` from repo root, then set `PYTHONPATH="$PWD"` when running the server. |

---

## References

- [Reticulum Network Stack](https://reticulum.network/)
- [LXMF Messaging Protocol](https://github.com/markqvist/LXMF)
- [RNode Firmware](https://github.com/markqvist/RNode_Firmware)
- [M5Stack Cardputer](https://docs.m5stack.com/en/core/Cardputer)
- [µReticulum](https://github.com/markqvist/uReticulum)
