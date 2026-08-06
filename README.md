# LMAO — LoRa Mesh Communication POC

**Proof of Concept**: Bidirectional LoRa communication between a Raspberry Pi
server and an M5Stack Cardputer ADV client using the
[Reticulum](https://reticulum.network/) networking stack with
[LXMF](https://github.com/markqvist/LXMF) messaging.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        LoRa RF (868/915 MHz)                        │
│  ┌───────────────┐                          ┌──────────────────────┐│
│  │ Laptop/Desktop│                          │ M5Stack Cardputer    ││
│  │ Human Client  │  WiFi/AutoIface          │ ADV                  ││
│  │ (Python CLI)  │──┐                       │ ┌────────────────┐   ││
│  └───────────────┘  │                       │ │ µReticulum     │   ││
│                     │                       │ │ client         │   ││
│  ┌──────────────────┴────────────────────┐  │ └──────┬─────────┘   ││
│  │  K8s Cluster (Turing Pi 2)            │  │        │ SPI          ││
│  │                                      │  │ ┌──────┴─────────┐   ││
│  │  ┌────────────────────────┐          │  │ │ SX1262 LoRa    │   ││
│  │  │ lmao-server Deployment │          │  │ │ radio + ant    │   ││
│  │  │ (tp4, hostNetwork)     │◄──LoRa───┼──┼─┘                │   ││
│  │  │ RNS+LXMF+gRPC+NATS pub │    ▲     │  └──────────────────────┘│
│  │  └───────────┬────────────┘    │ USB │                          │
│  │              │          ┌──────┴───┐ │                          │
│  │              │          │ESP32     │ │                          │
│  │              │          │RNode     │ │                          │
│  │              │          │(tp4 USB) │ │                          │
│  │              │          └──────────┘ │                          │
│  │  ┌───────────┴─────────┐            │                          │
│  │  │ NATS JetStream      │            │                          │
│  │  │ lmao.messages.env   │            │                          │
│  │  └───────────┬─────────┘            │                          │
│  │  ┌───────────┴─────────┐            │                          │
│  │  │ IoT Ingest Pod      │            │                          │
│  │  │ (NATS → DuckDB)     │            │                          │
│  │  └─────────────────────┘            │                          │
│  └─────────────────────────────────────┘                          │
└─────────────────────────────────────────────────────────────────────┘
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
protoc --python_out=. proto/lma_messages.proto proto/lma_grpc.proto
# The generated files will be at proto/lma_messages_pb2.py and proto/lma_grpc_pb2.py

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

> **Note:** The optional `nats-py` package is required for NATS JetStream
> queue features. Install with `pip install nats-py`.

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
- `DEST_HASH` is injected **automatically** by `bazel run //tools:install_all`
  (and by the E2E test): the tool loads or creates the server's persisted
  identity at `~/.local/share/lmao_server/lxmf/identity`, derives the
  `lxmf.delivery` destination hash, and uploads a patched `config.py` to the
  device — the source tree is never modified. You only need to set it by
  hand for fully manual flashing without the automated workflow. Obtain the
  hash from the server startup log (`Delivery destination (client DEST_HASH): ...`).
  Leave as `None` (default) to skip sending.
- Optionally adjust `NODE_NAME` and `DEBUG` level
- Optionally adjust `INTERVAL_SECONDS` (how often the Cardputer sends sensor data).
  Default 60s = 1 reading per minute. Minimum 10s (clamped automatically) to
  avoid LoRa congestion.
- To attach an external Grove I2C humidity/temperature sensor (e.g., DHT20),
  set `SENSOR_TYPE = "DHT20"` and `SENSOR_I2C_ADDR = 0x38`. Leave
  `SENSOR_TYPE = None` (default) to send only the ESP32's internal die temperature.

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

**Option C — Unified flash (install_all)**:

Flash both Cardputer client and RNode firmware in a single command.

```bash
# Auto-detect both devices and flash
bazel run //tools:install_all

# Specify explicit ports
bazel run //tools:install_all -- --cardputer-port /dev/ttyACM0 --rnode-port /dev/ttyUSB0

# Skip one device type
bazel run //tools:install_all -- --skip-cardputer
bazel run //tools:install_all -- --skip-rnode

# Custom client root path
bazel run //tools:install_all -- --client-root /path/to/cardputer_client

# Also deploy Pi server and K8s services
# (internal services are released through the local Docker registry at
#  192.168.0.36:5000 and deployed via Docker from the registry image)
bazel run //tools:install_all -- --include-services
bazel run //tools:install_all -- --include-services --skip-server
bazel run //tools:install_all -- --include-services --skip-k8s
bazel run //tools:install_all -- --include-services --skip-iot-ingest

# Set up local Docker registry (see §13)
bazel run //tools:install_all -- --setup-registry
bazel run //tools:install_all -- --setup-registry --include-services

# Skip DEST_HASH injection (e.g. server runs on a different host)
bazel run //tools:install_all -- --skip-dest-hash
```

The Cardputer flash automatically injects the server's `DEST_HASH`
(destination hash of the server's persisted identity) into the on-device
`config.py`. The identity is read from the local
`~/.local/share/lmao_server/lxmf/identity` when present, or synced from
the in-cluster server's PVC via `kubectl exec` when the server runs in
Kubernetes (issue #93) — flashed clients keep working across server
restarts and redeploys (issue #70).

Output shows a per-device summary table with OK/FAIL/SKIP status:

```
============================================================
  INSTALL SUMMARY
============================================================
  [OK]    Cardputer     — Flashed 42 file(s) to Cardputer
  [OK]    RNode (Heltec) — RNode firmware already installed
============================================================
  All detected devices processed successfully.
```

The tool auto-detects connected hardware via USB and exits with code 1
if any device fails.

### 5a. Cardputer ADV Hardware Reference

The target device is **M5Stack Cardputer ADV** (Stamp-S3A, ESP32-S3FN8) with
a **Cap LoRa-1262** module (SX1262) connected via the rear EXT 2.54-14P header.

**EXT 14-pin header pinout** (verified from Cardputer ADV schematic v1.0):

| Pin | Function | GPIO | Notes |
|-----|----------|------|-------|
| 1   | RESET    | 3    | SX1262 reset |
| 2   | INT      | 4    | SX1262 DIO1 (IRQ) |
| 3   | BUSY     | 6    | SX1262 busy |
| 4   | SCK      | 40   | SPI clock (MTDO — JTAG pin, reclaimed at boot) |
| 5   | MOSI     | 14   | SPI data |
| 6   | MISO     | 39   | SPI data (MTCK — JTAG pin, reclaimed at boot) |
| 7   | CS       | 5    | SPI chip select |
| 8   | TX       | 15   | UART (GPS) |
| 9   | RX       | 13   | UART (GPS) |
| 10  | SCL      | 8    | I2C clock |
| 11  | SDA      | 9    | I2C data |
| 12  | 5VOUT    | —    | 5V output |
| 13  | GND      | —    | Ground |
| 14  | 5VIN     | —    | 5V input |

**Key ESP32-S3 considerations:**

- **GPIO39 (MTCK) and GPIO40 (MTDO)** are JTAG pins on the ESP32-S3. The
  internal USB JTAG controller claims them by default for debugging. The
  LoRa interface driver creates `Pin()` objects for these pins **before**
  SPI init to reclaim them for GPIO/SPI use. This is handled automatically
  in `lib/urns/interfaces/lora.py`.
- **SPI bus 2 (HSPI / SPI3_HOST)** is used for the LoRa radio, separate from
  SPI bus 1 (FSPI / SPI2_HOST) used by the ST7789 display.
- **TCXO startup**: The Cap LoRa-1262 module needs 5000us for the TCXO to
  stabilize (configured via `dio3_tcxo_start_time_us` in `lora_boards.py`).
- The module connects via BOTH the HY2.0-4P Grove port (power) and the
  EXT 14-pin header (SPI data signals). Both must be firmly seated.

**Radio parameters** (must match server RNode config):

| Parameter | Value |
|-----------|-------|
| Frequency | 868 MHz (EU) / 915 MHz (US) |
| Spreading Factor | 7 |
| Bandwidth | 125 kHz |
| Coding Rate | 4:5 |
| TX Power | 14 dBm |
| Preamble | 24 symbols (must match RNode firmware's dynamic preamble — 8 symbols loses ~80% of RX packets) |
| Syncword | 0x1424 (Reticulum default) |

See `cardputer_client/lora_boards.py` for the `cardputer_adv` board preset
and `cardputer_client/config.py` for the LoRa interface configuration.

### 6. Test Communication

An automated E2E test can verify the full LoRa communication path with
both devices connected:

```bash
bazel test //tests:test_cardputer_lora_e2e --test_output=all
```

The test auto-skips when hardware is not detected.  See
[Section 11](#11-run-tests) for all test targets.

Manual verification steps:

1. Both devices powered on and within LoRa range
2. Cardputer sends "Hello from Cardputer — seq 1" at the configured interval (default: 60s, configurable via `INTERVAL_SECONDS` in `config.py`, minimum: 10s)
3. Server displays: `MSG from <hash>: Hello from Cardputer`
4. Server replies: `ACK from LMAO Server — received your message`
5. Cardputer displays the reply on screen

### 7. gRPC API (K8s Pod Integration)

The server exposes a gRPC API on port `50051` for K8s pods and other
automated clients to interact with the LoRa mesh programmatically.

> **Note:** The LMAO Server also **publishes incoming sensor data to
> an in-cluster NATS JetStream** for durable, at-least-once delivery
> to K8s consumers (e.g. the IoT Ingest pod).  See [Section 10](#10-nats-jetstream--in-cluster-durable-queueing).

**Proto definition**: [`proto/lma_messages.proto`](proto/lma_messages.proto)

| RPC | Type | Description |
|-----|------|-------------|
| `Send` | Unary | Inject a protobuf `LMAOEnvelope` into the LXMF mesh addressed to `destination_hash` |
| `Subscribe` | Server-streaming | Stream incoming LXMF messages to the client; optional `title_filter` |
| `Tunnel` | Bidirectional-streaming | Bidirectional raw LXMF packet tunnel (not yet implemented) |
| `GetIdentity` | Unary | Return the server's Reticulum identity hex and node name |

**Example** (Python):

```python
import grpc
from proto import lma_messages_pb2, lma_grpc_pb2_grpc

channel = grpc.insecure_channel("localhost:50051")
stub = lma_grpc_pb2_grpc.LMAOStub(channel)

# Send a message
stub.Send(lma_messages_pb2.SendRequest(
    envelope=envelope_bytes,
    destination_hash="a1b2c3d4..."
))

# Subscribe to incoming messages
for msg in stub.Subscribe(lma_messages_pb2.SubscribeRequest(title_filter="p:Envelope")):
    print(f"Received {len(msg.envelope)} bytes from {msg.source_hash}")

# Get server identity
identity = stub.GetIdentity(lma_messages_pb2.GetIdentityRequest())
print(f"Server: {identity.identity_hex}")
```

See [`k8s-app/iot_ingest.py`](k8s-app/iot_ingest.py) for a complete example.

### 8. Docker Image

The server Docker image is the release artifact for the in-cluster
Deployment (§9). Internal services are always released through the
[local Docker registry](#13-local-docker-registry) (`192.168.0.36:5000`);
`bazel run //tools:install_all -- --include-services` performs the full
build → push → `kubectl apply` cycle automatically.

```bash
# Build the image
docker build -t lmao-server .

# Push to the local registry
docker tag lmao-server 192.168.0.36:5000/lmao-server:latest
docker push 192.168.0.36:5000/lmao-server:latest
```

Running the container standalone (e.g. on a laptop for development) still
works — `--network host` is required for Reticulum AutoInterface multicast,
and `--device` passes the RNode through:

```bash
docker run --network host --device /dev/ttyUSB0:/dev/ttyUSB0 \
  -e NATS_SERVER=nats://192.168.0.43:30146 \
  -e LMAO_RNODE_PORT=/dev/ttyUSB0 \
  -v ~/.local/share/lmao_server:/root/.local/share/lmao_server \
  lmao-server
```

> **Legacy:** the server used to run in production as a Docker container +
> systemd unit on the Raspberry Pi. That deployment was retired when the
> server moved into the K8s cluster (issue #93); the install tooling now
> tears it down best-effort during deploys.

### 9. Kubernetes Deployment (Production)

The LMAO server runs **inside the K8s cluster** as a single-replica
Deployment on the node where the LoRa RNode is physically attached
(issue #93). Manifest: [`k8s/lmao-server.yaml`](k8s/lmao-server.yaml).

```bash
# Deploy / update (image must be pushed to the registry first — §8)
kubectl apply -f k8s/lmao-server.yaml
kubectl rollout status deployment/lmao-server

# Logs (look for "Delivery destination (client DEST_HASH): ...")
kubectl logs -f deployment/lmao-server
```

**Design:**

| Aspect | Choice | Why |
|--------|--------|-----|
| Node placement | `nodeSelector: kubernetes.io/hostname=tp4` | The RNode is plugged into tp4's USB. No HA failover is possible — USB radio is inherently node-bound. |
| USB passthrough | `hostPath: /dev/ttyUSB0` + `privileged: true` | K8s has no native USB serial support. A device plugin (e.g. generic-device-plugin) would avoid privileged mode if ever needed. After an RNode replug/node reboot, restart the pod: `kubectl rollout restart deployment/lmao-server`. |
| Networking | `hostNetwork: true` | Reticulum AutoInterface needs UDP multicast on the LAN (human_client discovery). `LMAO_AUTOIFACE_DEVICES=wlan0` keeps it off the flannel/veth CNI interfaces. gRPC is also reachable on the node IP `192.168.0.44:50051`. |
| Identity | PVC `lmao-server-identity` mounted at `/data`, `LMAO_SERVER_IDENTITY_PATH=/data/lxmf` | The LXMF identity must survive pod restarts or every Cardputer's baked DEST_HASH silently stops matching (issue #70). **The PVC was pre-populated from the old Pi server during migration**; local backup: `~/lmao-identity-backup-*`. |
| RNS state | `LMAO_RNS_TRANSPORT_PATH=/data/transport` (same PVC) | Persists known destinations/identities so ACK replies keep working across pod restarts without waiting for client re-announces. |
| NATS | `NATS_SERVER=nats://nats-server.default.svc.cluster.local:4222` | In-cluster ClusterIP (no NodePort hop). |
| gRPC discovery | ClusterIP Service `lmao-server` | Pods connect via `lmao-server.default.svc.cluster.local:50051` — same DNS name as before. |

The example K8s app at [`k8s-app/iot_ingest.py`](k8s-app/iot_ingest.py)
can be used from any pod to interact with the server:

```bash
export LMAO_SERVER=lmao-server.default.svc.cluster.local:50051
python k8s-app/iot_ingest.py --send --get-identity
```

> For in-cluster durable message queuing, see [Section 10](#10-nats-jetstream--in-cluster-durable-queueing)
> for NATS JetStream deployment and usage.

#### Environment Variables

The server respects the following environment variables (also configurable
when running in Docker):

| Variable | Default | Description |
|----------|---------|-------------|
| `NATS_SERVER` | `nats://nats-server.default.svc.cluster.local:4222` (K8s manifest) / `nats://localhost:4222` (code) | NATS JetStream URL |
| `LMAO_RNODE_PORT` | auto-detect | Serial port for RNode LoRa interface |
| `LMAO_SERVER_IDENTITY_PATH` | `~/.local/share/lmao_server/lxmf` | LXMF identity storage dir (PVC: `/data/lxmf`) |
| `LMAO_RNS_TRANSPORT_PATH` | unset (temp dir) | Reticulum state dir; set = persistent configdir (PVC: `/data/transport`) |
| `LMAO_AUTOIFACE_DEVICES` | unset (all interfaces) | Comma-separated interface allowlist for AutoInterface (e.g. `wlan0`) |
| `LMAO_MQTT_HOST` | `localhost` | MQTT broker hostname (IoT ingest) |
| `LMAO_MQTT_PORT` | `1883` | MQTT broker port |
| `LMAO_INGEST_DUCKDB_PATH` | `/data/sensors.db` | DuckDB file path (IoT ingest) |
| `QUERY_PORT` | `8080` | HTTP query API listen port (in-cluster) |
| `QUERY_MAX_ROWS` | `1000` | Max rows returned by `POST /query` |
| `QUERY_TIMEOUT` | `10` | Per-query statement timeout in seconds |

### 10. NATS JetStream — In-Cluster Durable Queueing

NATS JetStream runs **inside the K8s cluster** and provides durable,
at-least-once message delivery for the IoT sensor pipeline.

**Architecture overview:**

| Component | Where it runs | Role |
|-----------|---------------|------|
| **NATS Server** | K8s pod (`deployment/nats-server`) | Message broker with disk persistence (1Gi PVC) |
| **IoT Ingest** | K8s pod (`deployment/iot-ingest-consumer`) | Subscribes to sensor data, persists to DuckDB at `/data/sensors.db` |
| **LMAO Server** | K8s pod (`deployment/lmao-server`, on tp4) | Publishes incoming sensor data to NATS via ClusterIP |

**Data flow:**

```
Cardputer ──LoRa──→ RNode ──USB──→ tp4 ──pod──→ LMAO Server (in-cluster)
                                                     │
                                                publishes to
                                                     ▼
                              ┌─────────────────────────────────┐
                              │  K8s Cluster                    │
                              │  NATS JetStream                 │
                              │  lmao.messages.env              │
                              └──────────┬──────────────────────┘
                                         │ subscribes
                                         ▼
                              ┌─────────────────────┐
                              │  IoT Ingest Pod     │
                              │  (DuckDB store)     │
                              └─────────────────────┘
```

#### Deploy NATS

```bash
# Deploy NATS with JetStream persistence
kubectl apply -f k8s/nats-server.yaml

# Verify it's running
kubectl get pods -l app=nats-server
kubectl logs deployment/nats-server
```

#### Connect from inside the cluster

Pods inside the cluster (including the LMAO server Deployment) connect via
the ClusterIP DNS name:

```
nats://nats-server.default.svc.cluster.local:4222
```

#### Connect from outside the cluster

External hosts (e.g. a laptop running tools) can reach the in-cluster NATS
via the **NodePort** exposed on the K8s worker node:

```bash
export NATS_SERVER=nats://192.168.0.43:30146
```

The NodePort (`30146`) is defined in `k8s/nats-server.yaml`.  Adjust the IP
to your worker node's LAN address.

#### Deploy IoT Ingest Consumer

```bash
kubectl apply -f k8s/iot-ingest.yaml
```

The IoT Ingest pod subscribes to `lmao.messages.>` on the in-cluster NATS
and stores validated SensorReport payloads into DuckDB at `/data/sensors.db`
(backed by a 1Gi PVC).

#### Deploy Pipeline Monitor (silence alerting)

The `k8s/pipeline-monitor.yaml` CronJob checks every hour whether the
``LMAO_MESSAGES`` JetStream stream has received a message within
``SILENCE_THRESHOLD_HOURS`` (default 3). On silence it logs
``PIPELINE SILENCE`` and fails the job (exit 1); it publishes a
best-effort alert to ``ALERT_SUBJECT`` (default ``lmao.alerts.silence``).

```bash
kubectl apply -f k8s/pipeline-monitor.yaml
```

Configuration (ConfigMap ``pipeline-monitor-config``):

| Env var | Default | Description |
|---------|---------|-------------|
| ``NATS_SERVER`` | ``nats://nats-server.default.svc.cluster.local:4222`` | NATS JetStream URL |
| ``SILENCE_THRESHOLD_HOURS`` | ``3`` | Max gap since last message (hours) |
| ``ALERT_SUBJECT`` | ``lmao.alerts.silence`` | NATS subject for silence alerts |

Exit codes: ``0`` = healthy, ``1`` = silent or fatal error.

#### Using `NatsQueue` from Python

#### Using `NatsQueue` from Python

The `lma_core.queue` module provides an async `NatsQueue` wrapper that mirrors
the existing codebase conventions:

```python
import asyncio
from lma_core.queue import NatsQueue

async def main():
    nq = NatsQueue()

    # Connect to the in-cluster NATS server
    await nq.connect("nats://nats-server.default.svc.cluster.local:4222")

    # Create a stream (idempotent — safe to call every startup)
    await nq.ensure_stream("TELEMETRY", ["telemetry.>"])

    # Publish a protobuf-encoded envelope
    await nq.publish("telemetry.env", envelope_bytes)

    # Subscribe with durable consumer + queue group
    async def handle(msg):
        print(f"Got {len(msg.data)} bytes on {msg.subject}")

    await nq.subscribe("telemetry.>", "my-pod", handle)

    await nq.close()

asyncio.run(main())
```

#### Example: `iot_ingest.py --use-nats`

The example K8s app supports an optional `--use-nats` flag that switches from
gRPC to NATS for send and subscribe operations:

```bash
# Publish to NATS
python k8s-app/iot_ingest.py --use-nats --send

# Subscribe via NATS (durable consumer, queue group)
python k8s-app/iot_ingest.py --use-nats --subscribe --subscribe-timeout 10

# Override the NATS server address
NATS_SERVER=nats://localhost:4222 python k8s-app/iot_ingest.py --use-nats --send
```

#### Persistent DuckDB Storage

Messages consumed via NATS can be persisted to a local DuckDB database for
offline query and analysis. The IoT ingest app supports three flags:

- `--store`: Enable DuckDB persistence (requires `--subscribe --use-nats`)
- `--db-path PATH`: Database file path (default: `/data/sensors.db` or `$DUCKDB_PATH`)
- `--query SQL`: Run a read-only SQL query against the store and exit

```bash
# Subscribe with DuckDB persistence
python k8s-app/iot_ingest.py --use-nats --subscribe --store --subscribe-timeout 30

# Query stored data (no NATS connection needed)
python k8s-app/iot_ingest.py --query "SELECT node_id, count(*) FROM sensor_readings GROUP BY node_id"
```

#### Persistent Consumer Deployment

A long-lived Kubernetes Deployment (``k8s/iot-ingest.yaml``) runs a
persistent NATS→DuckDB consumer that **replaces the CLI-based approach**
for production use. The consumer auto-restarts on crash, persists DuckDB
data to a PersistentVolumeClaim, and uses a durable consumer name for
at-least-once delivery across restarts.

```bash
# Deploy the persistent consumer (requires NATS already deployed)
kubectl apply -f k8s/iot-ingest.yaml

# Or deploy via the unified installer
bazel run //tools:install_all -- --include-services
```

> **Using the local registry:** The Deployment manifest references the
> [local Docker registry](#13-local-docker-registry) image
> (`192.168.0.36:5000/lmao-iot-ingest:latest`) directly.  Release a new
> version by pushing the image, then re-apply:
> ```bash
> ./docker/registry/manage.sh push-ingest
> kubectl apply -f k8s/iot-ingest.yaml
> ```
> This replaces the manual `docker save | k3s ctr image import -` workflow.
> See [Section 13](#13-local-docker-registry) for full setup instructions.

| Variable | Default | Description |
|----------|---------|-------------|
| ``NATS_SERVER`` | ``nats://nats-server.default.svc.cluster.local:4222`` | NATS server URL |
| ``DUCKDB_PATH`` | ``/data/sensors.db`` | Path to DuckDB database file (on PVC) |
| ``CONSUMER_NAME`` | ``iot-ingest`` | Durable consumer name for JetStream |

**Graceful shutdown**: The consumer handles SIGTERM/SIGINT, drains the
subscription, and closes both NATS and DuckDB connections cleanly before
exiting. Kubernetes waits for ``terminationGracePeriodSeconds`` (default 30s)
before force-killing.

**PVC persistence**: DuckDB data is stored on a 1 Gi ``PersistentVolumeClaim``
(``iot-ingest-pvc``), surviving pod restarts and redeployments.

> **Tip**: Use ``--skip-iot-ingest`` to exclude the persistent consumer from
> the unified installer:
> ```bash
> bazel run //tools:install_all -- --include-services --skip-iot-ingest
> ```

## Querying stored data

The `iot-ingest-consumer` pod exposes an HTTP query API on port 8080.
Access it via `kubectl port-forward`:

```bash
kubectl port-forward svc/iot-query 8080:8080
```

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/query` | Execute a read-only SQL query (SELECT/WITH/EXPLAIN/SHOW/DESCRIBE only) |
| `GET`  | `/tables` | List all tables with row counts |
| `GET`  | `/schema/<table>` | List columns (name, type) for a table |
| `GET`  | `/healthz` | Health check for K8s probes |

### Examples

```bash
# Query recent sensor readings
curl -s -X POST localhost:8080/query \
  -H "Content-Type: application/json" \
  -d '{"sql": "SELECT node_id, value, unit, timestamp_ms FROM sensor_readings ORDER BY timestamp_ms DESC LIMIT 5"}' | python -m json.tool

# See response format:
# {
#   "columns": ["node_id", "value", "unit", "timestamp_ms"],
#   "rows": [["a1b2c3...", 22.5, "C", 1700000000000], ...],
#   "row_count": 5
# }

# List all tables
curl -s localhost:8080/tables | python -m json.tool

# Get schema for a table
curl -s localhost:8080/schema/text_messages | python -m json.tool
```

### Registered tables

| Table | Message Type | Proto Field | Description |
|-------|-------------|-------------|-------------|
| `sensor_readings` | SensorReport | sensor (10) | Temperature, humidity, and other sensor readings |
| `text_messages` | TextMessage | text (20) | Human-to-human LoRa text messages |
| `command_acks` | CommandAck | ack (12) | Command acknowledgment status |

### Caveats

- **Read-only only**: INSERT, UPDATE, DELETE, DROP, ATTACH, COPY, EXPORT, INSTALL, LOAD, and multi-statement queries are rejected.
- **No authentication**: In-cluster only, same trust model as the NATS server. Do not expose port 8080 outside the cluster.
- **Row cap**: Default 1000 rows per query (configurable via `QUERY_MAX_ROWS` env var).
- **Statement timeout**: 10s default (configurable via `QUERY_TIMEOUT`), enforced at the HTTP request level.
- **DuckDB cursors**: Each query creates a fresh cursor from the shared connection — reads do not block writes.

#### Architecture notes

- **No changes to gRPC**: The LMAO server and gRPC API are unchanged. NATS is
  additive and independent.
- **Publish-side auto-recovery**: The server's NATS client is configured with
  infinite reconnect attempts (nats-py otherwise gives up after ~2 min), so
  publishing resumes by itself after arbitrarily long NATS outages. If the
  client ever closes permanently, the server's publish path recreates the
  connection (with exponential backoff + jitter) instead of requiring a
  restart (issue #85). Messages received during an outage are still dropped
  — JetStream durability only helps consumers, not publishers.
- **No authentication (MVP)**: NATS runs without auth inside the cluster.
  Token auth is a 2-line ConfigMap change.
- **Single-node**: One NATS replica is deployed. For production, a 3-node
  NATS cluster can be added with minimal YAML changes.
- **Future bridge**: A gRPC-to-NATS bridge pod could subscribe to the LMAO
  server's gRPC stream and republish all messages to NATS, allowing pods to
  use NATS as their sole message source.

### 11. Run Tests

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

### 11a. Archon Workflows (AI Feature-to-PR)

Dedicated, versioned Archon workflows for this repo live in `.archon/workflows/`.
They encode the project rules from `AGENTS.md` (esptool bans, Bazel BUILD
completeness, mandatory hardware E2E with results in the PR body, production
health check). Prefer them over the generic bundled workflows:

```bash
archon workflow run lmao-fix-issue "Fix issue #N"     # issue → gated fix → PR
archon workflow run lmao-feature-dev "Add X to Y"     # idea → gated feature → PR
```

See [docs/archon-workflows.md](docs/archon-workflows.md) for the gate chain,
commands, and design rationale (issue #89).

### 12. Run the Human Client

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

### 13. Local Docker Registry

A **self-hosted Docker registry** runs on the Pi server (`selfhost`, `192.168.0.36:5000`)
for local image storage and distribution to the K3s cluster. This eliminates the need to
pull from Docker Hub on cluster nodes or use the manual `docker save | k3s ctr image import -`
workflow.

#### Quick start

```bash
# 1. Start the registry
./docker/registry/manage.sh start

# 2. Build and push all LMAO images to the registry
./docker/registry/manage.sh push

# 3. Verify
curl http://192.168.0.36:5000/v2/_catalog
# → {"repositories":["lmao-server","lmao-iot-ingest"]}
```

The registry runs as a Docker container managed by docker-compose and restarts
automatically on reboot (`restart: unless-stopped`).

#### Usage

```bash
# Start / stop
./docker/registry/manage.sh start
./docker/registry/manage.sh stop

# Build & push images
./docker/registry/manage.sh push            # all images
./docker/registry/manage.sh push-server     # lmao-server only
./docker/registry/manage.sh push-ingest     # lmao-iot-ingest only

# Inspect
./docker/registry/manage.sh list            # list images + tags
./docker/registry/manage.sh status          # container + API health
./docker/registry/manage.sh k3s-config      # print K3s registries.yaml
```

#### Pushing images

```bash
docker tag lmao-server 192.168.0.36:5000/lmao-server:latest
docker push 192.168.0.36:5000/lmao-server:latest
```

#### Pulling from the Pi itself

The Pi's Docker daemon is configured to trust `192.168.0.36:5000` as an insecure
registry (see `/etc/docker/daemon.json`). Images pushed to the registry are
immediately pullable on the Pi without any extra setup.

#### Pulling from K3s cluster nodes

For cluster nodes to pull from the local registry, place this file at
`/etc/rancher/k3s/registries.yaml` **on every node** and restart K3s:

```bash
# On control-plane nodes:
sudo cp k3s-registries.yaml /etc/rancher/k3s/registries.yaml
sudo systemctl restart k3s

# On worker nodes:
sudo cp k3s-registries.yaml /etc/rancher/k3s/registries.yaml
sudo systemctl restart k3s-agent
```

Or generate the config with the helper:

```bash
./docker/registry/manage.sh k3s-config | sudo tee /etc/rancher/k3s/registries.yaml
```

The config tells containerd to reach the Pi's registry (`192.168.0.36:5000`)
via plain HTTP. After restarting K8s services, update your Deployments to
reference `192.168.0.36:5000/lmao-server:latest` instead of `lmao-server:latest`.

#### Deploying from the registry

```yaml
# In your K8s Deployment YAML:
image: 192.168.0.36:5000/lmao-server:latest
imagePullPolicy: Always
```

#### Script reference

| Command | Description |
|---------|-------------|
| `start` | Start the registry container |
| `stop` | Stop the registry container |
| `push` | Build & push all LMAO images |
| `push-server` | Build & push lmao-server only |
| `push-ingest` | Build & push lmao-iot-ingest only |
| `list` | List images and tags in the registry |
| `status` | Check container and API health |
| `k3s-config` | Print `registries.yaml` for cluster nodes |

#### Configuration

The registry is configured via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `REGISTRY_HOST` | `192.168.0.36` | Registry hostname/IP |
| `REGISTRY_PORT` | `5000` | Registry port |

---

## Project Structure

```
├── README.md                          # This file
├── ARCHITECTURE.md                    # Full system architecture reference
├── AGENTS.md                          # Project rules (E2E flash verification)
├── Dockerfile                         # Container build for server deployment
├── Dockerfile.iot-ingest              # Container build for IoT ingest consumer
├── .bazelversion                      # Bazel version pin (7.4.1)
├── MODULE.bazel                       # Bazel module definition
├── .archon/                           # Versioned Archon config (see docs/archon-workflows.md)
│   ├── workflows/                     # LMAO-specific workflows (lmao-fix-issue, lmao-feature-dev)
│   │                                  # + pi-adapted overrides of bundled workflows
│   └── commands/                      # LMAO gate commands (lmao-validate, lmao-hardware-e2e, ...)
│
├── proto/                             # Canonical protobuf schema (single source of truth)
│                                     # (moved from lmao_server/proto/ — now generated by Bazel)
│   ├── BUILD                          # Bazel: proto_library + py_proto_library targets
│   ├── lma_messages.proto             # Protobuf schema (LMAO mesh message types)
│   ├── lma_grpc.proto                 # Protobuf schema (gRPC service types)
│   ├── __init__.py                    # Package marker
│   ├── lma_messages_pb2.py            # Generated protobuf Python stubs
│   └── lma_grpc_pb2.py                # Generated gRPC Python stubs
│
├── lma_core/                          # Shared Python wrapper library
│   ├── BUILD                          # Bazel: py_library target
│   ├── __init__.py                    # Re-exports generated protobuf stubs
│   ├── config_utils.py                # RNode port resolution + INI generation helpers
│   ├── message_utils.py               # Shared LXMF message decoding (decode_lmao_message)
│   ├── query_api.py                   # Embedded HTTP query API (aiohttp) + SQL guard
│   ├── queue.py                       # Async NATS JetStream wrapper (NatsQueue)
│   ├── storage.py                     # Async DuckDB persistent store (DuckDbStore)
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
│   ├── lmao-server.yaml               # In-cluster server: Deployment + identity PVC + Service
│   ├── nats-server.yaml               # NATS Deployment + Service + ConfigMap (JetStream)
│   └── iot-ingest.yaml                # Persistent IoT Ingest Consumer (NATS→DuckDB)
│
├── k8s-app/                           # Example K8s pod application
│   ├── iot_ingest.py                  # gRPC + NATS client: Send + Subscribe + GetIdentity
│   └── iot_ingest_consumer.py         # Persistent consumer service (NATS JetStream → DuckDB)
│
├── cardputer_client/                  # MicroPython — runs on M5Stack Cardputer
│   ├── boot.py                        # MicroPython boot script (sets /lib in path)
│   ├── config.py                      # µReticulum config for onboard LoRa
│   ├── main.py                        # Client: periodic hello + reply display
│   ├── lib/                           # Vendored µReticulum library (urns port)
│   └── proto/
│       ├── BUILD                      # Bazel: py_library for host-side tests
│       ├── lma_messages.proto         # Same protobuf schema (reference)
│       └── lma_encoder.py             # Hand-coded minimal encoder (no protobuf dep)
│
├── tests/                             # Host-side tests (Bazel py_test targets)
│   ├── BUILD                          # Bazel: py_test targets
│   ├── conftest.py                    # Shared mock helpers (setup_common_mocks / cleanup_common_mocks)
│   ├── test_config.py                 # Config module unit tests (no hardware)
│   ├── test_lma_core.py               # lma_core import error handling + exports
│   ├── test_lma_encoder.py            # Encoder round-trip + cross-validation tests
│   ├── test_queue.py                  # NatsQueue unit tests (mocked nats-py)
│   ├── test_query_api.py              # SQL guard + query API endpoint tests
│   ├── test_storage.py               # DuckDbStore unit tests (mocked duckdb)
│   ├── test_server_handler.py         # Server handler unit tests (mocked RNS/LXMF)
│   ├── test_server_startup.py         # Server startup lifecycle + async entry point tests
│   ├── test_client_repl.py            # Human client REPL input parsing tests
│   ├── test_client_startup.py         # Human client startup lifecycle tests
│   └── e2e/
│       └── test_cardputer_flash.py    # E2E flash + boot validation test
│
├── docker/                            # Docker infrastructure
│   └── registry/                      # Local Docker registry (self-hosted on Pi)
│       ├── docker-compose.yml         # Registry container + persistent volume
│       └── manage.sh                  # CLI helper: start/stop/push/list/k3s-config
│
├── tools/                             # Build/install tools
│   ├── BUILD                          # Bazel: py_binary + py_library targets
│   ├── install_all.py                 # Unified hardware flash orchestrator
│   └── install_services.py            # Pi server Docker build + K8s manifest apply
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
See [`proto/lma_messages.proto`](proto/lma_messages.proto) and [`proto/lma_grpc.proto`](proto/lma_grpc.proto) for the complete definitions.

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

> **Sensor Readings Convention:** Each `SensorReading` in a `SensorReport.readings[]`
> uses `sensor_id` to identify the measurement type: `sensor_id=1` = temperature (°C),
> `sensor_id=2` = humidity (%). New sensor types should use `sensor_id >= 3` and
> be documented here.

---

## Scope (POC Only)

This POC intentionally limits scope to:

- ✅ Direct LoRa communication (single-hop, no propagation)
- ✅ Text messages between Cardputer, RPi server, and Human Client (Python CLI)
- ✅ LXMF acknowledgements
- ✅ Protobuf-encoded payloads
- ✅ gRPC API for K8s pod integration (Send, Subscribe, GetIdentity)
- ✅ NATS JetStream queue for in-cluster pub/sub messaging
- ✅ Docker containerization
- ✅ K8s Service + Endpoints for external RPi discovery
- ❌ No multi-hop / store-and-forward
- ✅ WiFi fallback (AutoInterface enabled when RNode is not connected)
- ❌ No DuckDB storage in server.py
- ❌ No image/audio/file transfer
- ❌ No encryption key management
- ❌ No battery optimization

For the full system design, see [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## Troubleshooting

| Problem | Check |
|---------|-------|
| Server can't find RNode | Is ESP32 plugged in? Set `LMAO_RNODE_PORT` or check auto-detected port |
| In-cluster pod can't find RNode after node reboot/replug | Device may have re-enumerated — `kubectl rollout restart deployment/lmao-server`; verify `/dev/ttyUSB0` exists on tp4 |
| Server logs "Could not send reply (no source destination)" after a restart | The client identity cache was empty; the client re-announces on its next reboot. Persisted via `LMAO_RNS_TRANSPORT_PATH` on the PVC since issue #93 |
| `test_cardputer_lora_e2e` skips | The test needs a **locally attached** RNode + Cardputer. With the server running in K8s (RNode on tp4), it auto-skips on other hosts — run it on a machine with both devices plugged in |
| Server hangs with no output | RNode port not found — the server now warns and starts in WiFi-only mode. Check `LMAO_RNODE_PORT`. |
| No LoRa packets despite devices on same frequency | Check **all** radio parameters match: SF, BW, CR, and TXP (not just frequency) |
| Cardputer has µReticulum firmware, not MicroPython | That's expected with rsCardputer firmware — it's a valid LXMF client. Use Option B above. |
| No LoRa packets | Both devices on same frequency? In range? |
| Cardputer display blank | ST7789 driver installed? SPI pins correct? |
| Cardputer wedged (REPL OK but uploads stall at byte 0) | Flash tooling auto-recovers via `machine.reset()`/watchdog reset (#74); the client also arms a hardware watchdog (`WDT_TIMEOUT_MS` in `main.py`) so unattended wedges self-heal. Physical RESET is only needed if both fail. |
| Cardputer stops sending after ~1 h, still powered (serial shows `memory allocation failed`) | GC heap fragmentation from long-running urns traffic (#71) — free bytes remain but no contiguous 2 KiB block for inbound packets. The client gc.collects every cycle, probes the heap, and hard-resets itself when fragmented (also on `MemoryError` or 10 consecutive loop errors). If running older client code, re-flash. |
| Cardputer REPL unreachable (write timeouts, Ctrl+C ignored) but app keeps running over LoRa | **Tier 1**: `sudo USBDEVFS_RESET /dev/bus/usb/...` — restores USB data path but **not** the MicroPython REPL. **Tier 2** (remote recovery, issue #78): Send LoRa REBOOT command — `bazel run //lmao_server:send_command -- --target <node_identity_hex> --action reboot`. The Cardputer validates the command and hard-resets itself. **Tier 3**: Physical RESET button on the device — last resort. See [issue #78](https://github.com/pondycrane/LMAO/issues/78) for details on the root cause (USB-CDC ring-buffer overflow) and the stdin drain mitigation. |
| "Permission denied" on serial | `sudo usermod -a -G dialout $USER` |
| Protobuf import error | Bazel: run `bazel build //proto:lma_messages_py_proto //proto:lma_grpc_py_proto`. Without Bazel: run `protoc --python_out=. proto/lma_messages.proto proto/lma_grpc.proto` from repo root, then set `PYTHONPATH="$PWD"` when running the server. |

---

## References

- [Reticulum Network Stack](https://reticulum.network/)
- [LXMF Messaging Protocol](https://github.com/markqvist/LXMF)
- [RNode Firmware](https://github.com/markqvist/RNode_Firmware)
- [M5Stack Cardputer](https://docs.m5stack.com/en/core/Cardputer)
- [µReticulum](https://github.com/markqvist/uReticulum)
