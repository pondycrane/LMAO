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

### 1. Flash the ESP32 RNode

Follow the guide in [`rnode_firmware/README.md`](rnode_firmware/README.md).

After flashing, verify:

```bash
rnodeconf --port /dev/ttyUSB0 --info
```

### 2. Install Server Dependencies

On your Raspberry Pi:

```bash
cd lmao_server
pip3 install -r requirements.txt
```

### 3. Configure the Server

Edit `lmao_server/config.py`:
- Set the correct serial port (`/dev/ttyUSB0` or `/dev/ttyACM0`)
- Set the correct frequency for your region (868 MHz EU / 915 MHz US)

### 4. Start the Server

```bash
cd lmao_server
python3 server.py
```

Expected output:

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

### 5. Flash the Cardputer

Copy the `cardputer_client/` directory to your Cardputer (via Thonny, ampy, or
MicroPython WebREPL):

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

### 6. Test Communication

1. Both devices powered on and within LoRa range
2. Cardputer sends "Hello from Cardputer — seq 1" every 10 seconds
3. Server displays: `MSG from <hash>: Hello from Cardputer`
4. Server replies: `ACK from LMAO Server — received your message`
5. Cardputer displays the reply on screen

---

## Project Structure

```
├── README.md                          # This file
├── ARCHITECTURE.md                    # Full system architecture reference
│
├── lmao_server/                       # Python — runs on Raspberry Pi
│   ├── requirements.txt               # Python dependencies (rns, lxmf, protobuf)
│   ├── config.py                      # Reticulum config with RNode LoRa interface
│   ├── server.py                      # Main server: RNS + LXMF router + echo handler
│   └── proto/
│       ├── lma.proto                  # Protobuf schema (all message types)
│       └── lma_pb2.py                 # Generated Python protobuf stubs
│
├── cardputer_client/                  # MicroPython — runs on M5Stack Cardputer
│   ├── config.py                      # µReticulum config for onboard LoRa
│   ├── main.py                        # Client: periodic hello + reply display
│   └── proto/
│       ├── lma.proto                  # Same protobuf schema (reference)
│       └── lma_encoder.py             # Hand-coded minimal encoder (no protobuf dep)
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
- ❌ No WiFi fallback
- ❌ No sensor integration
- ❌ No image/audio/file transfer
- ❌ No encryption key management
- ❌ No battery optimization

For the full system design, see [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## Troubleshooting

| Problem | Check |
|---------|-------|
| Server can't find RNode | Is ESP32 plugged in? Correct port in config.py? |
| No LoRa packets | Both devices on same frequency? In range? |
| Cardputer display blank | ST7789 driver installed? SPI pins correct? |
| "Permission denied" on serial | `sudo usermod -a -G dialout $USER` |
| Protobuf import error | Run `pip3 install -r lmao_server/requirements.txt` |

---

## References

- [Reticulum Network Stack](https://reticulum.network/)
- [LXMF Messaging Protocol](https://github.com/markqvist/LXMF)
- [RNode Firmware](https://github.com/markqvist/RNode_Firmware)
- [M5Stack Cardputer](https://docs.m5stack.com/en/core/Cardputer)
- [µReticulum](https://github.com/markqvist/uReticulum)
