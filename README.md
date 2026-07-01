# LMAO — LoRa Mesh Communication POC

**Proof of Concept**: Bidirectional LoRa communication between a Raspberry Pi
server and an M5Stack Cardputer ADV client using the
[Reticulum](https://reticulum.network/) networking stack with
[LXMF](https://github.com/markqvist/LXMF) messaging.

---

## Architecture

```
┌─────────────────────┐         LoRa (868/915 MHz)        ┌──────────────────────┐
│                     │ ◄────────────────────────────────── │                      │
│  Raspberry Pi       │                                    │  M5Stack Cardputer   │
│  ┌───────────────┐  │                                    │  ADV                 │
│  │ LMAO Server   │  │                                    │  ┌────────────────┐  │
│  │ (Python)      │  │                                    │  │ µReticulum     │  │
│  │ RNS + LXMF    │  │                                    │  │ client         │  │
│  └──────┬────────┘  │                                    │  └──────┬─────────┘  │
│         │ USB       │                                    │         │ SPI         │
│  ┌──────┴────────┐  │                                    │  ┌──────┴─────────┐  │
│  │ ESP32 RNode   │  │                                    │  │ SX1262 LoRa    │  │
│  │ (LoRa bridge) │──┼──────── LoRa RF ──────────────────┼──│ radio + ant    │  │
│  └───────────────┘  │                                    │  └────────────────┘  │
└─────────────────────┘                                    └──────────────────────┘
```

## Quickstart

### Prerequisites

| Component | Requirements |
|-----------|-------------|
| Raspberry Pi | Python 3.8+, USB port |
| ESP32 RNode | Flashed with RNode firmware |
| Cardputer ADV | M5Stack Cardputer with LoRa antenna, MicroPython installed |
| LoRa band | Matching frequency (868 MHz EU / 915 MHz US) |
| Bazel | v7.4.1 (see `.bazelversion`) — use [bazelisk](https://github.com/bazelbuild/bazelisk) (auto-selects correct version via `.bazelversion`). Install: `npm install -g @bazel/bazelisk` ([other install methods](https://github.com/bazelbuild/bazelisk#installation)). Ensure `~/.npm-global/bin` (or your npm global bin dir) is in `PATH`. Verify with `bazel --version` (expected: `bazel 7.4.1`). |

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

### 5. Flash / Prepare the Cardputer

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
# Example using ampy
ampy --port /dev/ttyUSB1 put cardputer_client/config.py
ampy --port /dev/ttyUSB1 put cardputer_client/main.py main.py
ampy --port /dev/ttyUSB1 put cardputer_client/proto/lma_encoder.py proto/lma_encoder.py
```

The Cardputer will auto-run `main.py` on boot and display:

```
LMAO POC Ready
ID: a1b2c3d4...
```

**Option B — rsCardputer native firmware** (recommended, pre-built binary):

Flash the [rsCardputer](https://github.com/ratspeak/rsCardputer) dual-mode firmware
for a full LXMF messenger with display, keyboard, and LoRa support out of the box:

```bash
# Using esptool (download the full firmware zip first)
esptool.py --chip esp32s3 --port /dev/ttyACM0 write-flash 0x0 rscardputer-full.bin
```

See [rsCardputer README](https://github.com/ratspeak/rsCardputer) for details.
Either option works with the LMAO server — both use the same Reticulum/LXMF protocol.

### Radio Parameter Compatibility

**All LoRa devices must use identical radio parameters to communicate.**

The server defaults to fast/short-range settings. If using rsCardputer firmware
(which defaults to Long Fast: SF11, BW250 kHz), update the server config to match:

```python
# In lmao_server/config.py, update the RNode LoRa interface:
{
    "type": "RNodeInterface",
    "port": "/dev/ttyUSB0",
    "frequency": 868000000,
    "bandwidth": 250000,        # Match client
    "spreadingfactor": 11,       # Match client
    "codingrate": 5,
    "txpower": 17,
}
```

| Parameter | LMAO default (fast) | rsCardputer default (Long Fast) |
|-----------|--------------------|-------------------------------|
| SF | 7 | 11 |
| BW | 125 kHz | 250 kHz |
| Bitrate | 10.84 kbps | 1.07 kbps |
| Link budget | 143 dB | 153 dB |

Also see the [rsCardputer radio presets](https://github.com/ratspeak/rsCardputer?tab=readme-ov-file#radio-presets) for other options.

### 6. Test Communication

1. Both devices powered on and within LoRa range
2. Cardputer sends "Hello from Cardputer — seq 1" every 10 seconds
3. Server displays: `MSG from <hash>: Hello from Cardputer`
4. Server replies: `ACK from LMAO Server — received your message`
5. Cardputer displays the reply on screen

### 7. Run Tests

```bash
# Run all unit tests (no hardware required)
bazel test //tests:all

# Run a specific unit test
bazel test //tests:test_lma_encoder --test_output=all

# Run the E2E flash test (requires physical Cardputer hardware)
bazel test //tests:test_cardputer_e2e --test_output=all
```

The E2E test auto-skips when no Cardputer hardware is detected.

---

## Project Structure

```
├── README.md                          # This file
├── ARCHITECTURE.md                    # Full system architecture reference
├── .bazelversion                      # Bazel version pin (7.4.1)
├── MODULE.bazel                       # Bazel module definition
│
├── proto/                             # Canonical protobuf schema (single source of truth)
│                                     # (moved from lmao_server/proto/ — now generated by Bazel)
│   ├── BUILD                          # Bazel: proto_library + py_proto_library targets
│   └── lma.proto                      # Protobuf schema (all message types)
│
├── lma_core/                          # Shared Python wrapper library
│   ├── BUILD                          # Bazel: py_library target
│   └── __init__.py                    # Re-exports generated protobuf stubs
│
├── lmao_server/                       # Python — runs on Raspberry Pi
│   ├── BUILD                          # Bazel: py_binary target
│   ├── requirements.txt               # Python dependencies (rns, lxmf, protobuf)
│   ├── config.py                      # Reticulum config with RNode LoRa interface
│   └── server.py                      # Main server: RNS + LXMF router + echo handler
│
├── cardputer_client/                  # MicroPython — runs on M5Stack Cardputer
│   ├── config.py                      # µReticulum config for onboard LoRa
│   ├── main.py                        # Client: periodic hello + reply display
│   └── proto/
│       ├── BUILD                      # Bazel: py_library for host-side tests
│       ├── lma.proto                  # Same protobuf schema (reference)
│       └── lma_encoder.py             # Hand-coded minimal encoder (no protobuf dep)
│
├── tests/                             # Host-side tests (Bazel py_test targets)
│   ├── BUILD                          # Bazel: py_test targets
│   ├── test_lma_encoder.py            # Encoder round-trip + cross-validation tests
│   ├── test_server_handler.py         # Server handler unit tests (mocked RNS/LXMF)
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

The protobuf schema supports multiple message types (text, sensor, command, etc.).
This POC uses only `TextMessage`:

```protobuf
message LMAOEnvelope {
  oneof payload {
    TextMessage text = 20;
    // ... other types defined for future use
  }
}

message TextMessage {
  string node_id   = 1;
  string content   = 2;
  uint64 timestamp = 3;
}
```

Typical wire size: **45 bytes** for "Hello from Cardputer" — well within LoRa's
~200 B payload budget.

---

## Scope (POC Only)

This POC intentionally limits scope to:

- ✅ Direct LoRa communication (single-hop, no propagation)
- ✅ Text messages between Cardputer and RPi server
- ✅ LXMF acknowledgements
- ✅ Protobuf-encoded payloads
- ❌ No multi-hop / store-and-forward
- ✅ WiFi fallback (AutoInterface enabled when RNode is not connected)
- ❌ No sensor integration
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
