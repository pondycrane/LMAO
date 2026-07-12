# LMAO — LoRa Message Async Orchestrator

LMAO is a LoRa mesh messaging system that bridges Reticulum/LXMF messages
to NATS JetStream for downstream processing and persistence. It runs on a
Raspberry Pi with an ESP32 RNode LoRa interface.

## Architecture

```
Cardputer ──LoRa/LXMF──→ [LMAO Server] ──NATS──→ IoT Ingest ──→ DuckDB
                           ↑ systemd              consuming       persisting
                           │ auto-start
                      ┌────┴─────┐
                      │  gRPC    │  (optional streaming API)
                      └──────────┘
```

## Quick Start

### Full Deployment

```bash
# Build, start container, install systemd auto-start service
bazel run //tools:install_all -- --include-services
```

### Manual Docker Run

```bash
docker build -t lmao-server .
docker run -d --name lmao-server --restart unless-stopped \
  --network host \
  -e NATS_SERVER=nats://localhost:4222 \
  -e LMAO_RNODE_PORT=/dev/ttyACM0 \
  --device /dev/ttyACM0:/dev/ttyACM0 \
  lmao-server
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `NATS_SERVER` | `nats://localhost:4222` | NATS server URL for JetStream publishing |
| `LMAO_RNODE_PORT` | auto-detect | Serial port for RNode LoRa interface |
| `LMAO_MQTT_HOST` | `localhost` | MQTT broker hostname (IoT ingest) |
| `LMAO_MQTT_PORT` | `1883` | MQTT broker port |
| `LMAO_INGEST_DUCKDB_PATH` | `/data/sensors.db` | DuckDB file path (IoT ingest) |

## Service Management (systemd)

When deployed with `--include-services`, the server runs as a systemd service:

```bash
sudo systemctl start lmao-server      # Start the container
sudo systemctl stop lmao-server       # Stop the container
sudo systemctl status lmao-server     # Check status
sudo journalctl -u lmao-server -f     # Tail logs
sudo systemctl disable lmao-server    # Disable auto-start on boot
```

## K8s Deployment

The repository includes Kubernetes manifests for:
- **NATS Server**: `k8s/nats-server.yaml`
- **LMAO Service**: `k8s/lmao-service.yaml`
- **IoT Ingest Consumer**: `k8s/iot-ingest.yaml`

Apply with:

```bash
kubectl apply -f k8s/nats-server.yaml
kubectl apply -f k8s/lmao-service.yaml
kubectl apply -f k8s/iot-ingest.yaml
```

## Development

### Running Tests

```bash
# All tests
bazel test //tests:all

# Specific test file
bazel test //tests:test_server_handler
bazel test //tests:test_queue
bazel test //tests:test_install_all
```

### Prerequisites

- Python 3.10+
- Docker (for container builds)
- pip packages: `pip install nats-py grpcio grpcio-tools`
- RNode LoRa hardware (optional, for development without hardware)

## License

MIT
