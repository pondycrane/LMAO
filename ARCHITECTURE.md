# LMAO — Leave Me Alone Offgrid

## System Architecture (Reticulum/LXMF)

### Stack

```
Application   │  Chat · IoT Ingest · Command Dispatch · Call Signaling
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
| **LMAO Server** | RPi/NUC | LoRa + WiFi + TCP | Propagation Node · IoT processor · Command scheduler · Human services |
| **Human nodes** | Phone (Sideband) / Laptop (NomadNet/MeshChat) | WiFi (preferred) | Person-to-person: text · images · audio clips · calls |
| **Backbone** | Ubiquiti/MikroTik radios | Long-range WiFi | High-capacity between sites |

## Topology

```
  Sensors ──LoRa──→ RNode ──USB/WiFi──→ Central Server ←──WiFi mesh── Humans
  Actuators ←──LoRa── RNode                          ↕
                                              Propagation Node
                                            (store-and-forward)
```

- **LoRa leaf**: every device hears every packet; server moderates airtime
- **WiFi mesh**: human communication, image/audio transfers, call streams
- **Server bridges both worlds**: translates between high- and low-speed networks

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
| `//tests:test_lma_encoder` | Encoder compatibility tests |
| `//tests:test_server_handler` | Server handler tests |

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