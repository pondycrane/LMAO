#!/usr/bin/env python3
"""
Diagnose each layer of the LMAO communication stack.
Tests: RNode hardware → Reticulum → LXMF → Protobuf application

Reticulum is initialized once in Layer 1 and reused in Layer 2,
avoiding the "already registered destination" error from reinit.
"""

import os
import sys
import time
import shutil


def _identity_to_destination(identity):
    """Wrap an RNS.Identity in an RNS.Destination for LXMF."""
    from lma_core.rns_di import RNS
    return RNS.Destination(
        identity,
        RNS.Destination.OUT,
        RNS.Destination.SINGLE,
        "lxmf",
        "delivery",
    )


# ── Layer 0: RNode Hardware ──────────────────────────────────────────

print("=" * 60)
print("LAYER 0: RNode Hardware (Serial/USB)")
print("=" * 60)

serial_ports = ["/dev/ttyUSB0", "/dev/ttyACM0", "/dev/ttyUSB1", "/dev/ttyACM1"]

for port in serial_ports:
    if os.path.exists(port):
        try:
            import serial
            s = serial.Serial(port, 115200, timeout=2)
            time.sleep(0.3)
            # Send RNode probe (0x00)
            s.write(b"\x00")
            time.sleep(0.5)
            data = s.read(200)
            s.close()
            if len(data) > 0:
                print(f"  ✅ {port} — responds ({len(data)} bytes)")
                # Try to identify RNode protocol frames
                if data[0:1] == b"\xc0":
                    print(f"     → RNode protocol frame detected")
                else:
                    print(f"     → Raw data: {data[:40].hex()}")
            else:
                print(f"  ⚠️  {port} — open but no response")
        except Exception as e:
            print(f"  ❌ {port} — error: {e}")
    else:
        print(f"  ⚪ {port} — not found")


# ── Shared Reticulum state (used by Layer 1 and Layer 2) ─────────────

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lma_core.config_utils import RnsConfig
from lma_core.rns_di import RNS, LXMF

# Reticulum artifacts — cleaned up at the very end
_rns_configdir = None
_rns_identity = None
_lxmf_router = None
_lxmf_configdir = None


def _cleanup():
    """Final teardown of Reticulum / LXMF state."""
    if _lxmf_router is not None:
        try:
            _lxmf_router.shutdown()
        except Exception:
            pass
    if _lxmf_configdir is not None:
        shutil.rmtree(_lxmf_configdir, ignore_errors=True)
    try:
        RNS.Reticulum.get_instance().teardown()
    except Exception:
        pass
    if _rns_configdir is not None:
        shutil.rmtree(_rns_configdir, ignore_errors=True)
    # Reset Reticulum singleton so subsequent runs are clean
    if hasattr(RNS.Reticulum, '_Reticulum__instance'):
        RNS.Reticulum._Reticulum__instance = None


# ── Layer 1: Reticulum Initialization ────────────────────────────────

print("\n" + "=" * 60)
print("LAYER 1: Reticulum Network Stack")
print("=" * 60)

try:
    if RNS is None:
        print("  ❌ RNS module not importable (not installed)")
        sys.exit(1)
    else:
        print(f"  ✅ RNS module loaded (version: {getattr(RNS, '__version__', '?')})")

        # Build config using the project's helper
        cfg = RnsConfig(transport_path="/tmp/lmao_diag_rns")
        _rns_configdir = cfg.get_configdir()
        print(f"  ✅ Config directory created at {_rns_configdir}")

        # Show the config content
        with open(os.path.join(_rns_configdir, "config")) as f:
            config_content = f.read()
        print(f"\n  Config content:\n{config_content}\n")

        # Initialize Reticulum
        rns_instance = RNS.Reticulum(configdir=_rns_configdir)
        print(f"  ✅ Reticulum initialized successfully")

        # Check interfaces
        rns_inst = RNS.Reticulum.get_instance()
        ifaces = []
        for attr_name in ['interfaces', '__interfaces', '_Reticulum__interfaces']:
            if hasattr(rns_inst, attr_name):
                raw = getattr(rns_inst, attr_name)
                if isinstance(raw, (list, tuple, dict)):
                    if isinstance(raw, dict):
                        ifaces = list(raw.values())
                    else:
                        ifaces = list(raw)
                    break

        if ifaces:
            print(f"  ✅ Active interfaces ({len(ifaces)}):")
            for iface in ifaces:
                name = getattr(iface, 'name', iface.__class__.__name__)
                connected = getattr(iface, 'is_connected', None) or getattr(iface, 'online', False)
                status = "✅" if connected else "⚠️"
                print(f"     {status} {iface.__class__.__name__}: {name}")
                if hasattr(iface, 'port'):
                    print(f"        Port: {iface.port}")
                if hasattr(iface, 'frequency'):
                    print(f"        Freq: {iface.frequency} Hz")
                if hasattr(iface, 'signal_strength'):
                    ss = getattr(iface, 'signal_strength', None)
                    print(f"        RSSI: {ss} dBm" if ss is not None else "        RSSI: N/A")
        else:
            print("  ⚠️  No interfaces found")

        # Create identity
        _rns_identity = RNS.Identity()
        print(f"  ✅ Identity created: {RNS.hexrep(_rns_identity.hash, delimit=False)}")

except ImportError as e:
    print(f"  ❌ Import error: {e}")
    _cleanup()
    sys.exit(1)
except Exception as e:
    print(f"  ❌ Reticulum init failed: {e}")
    import traceback
    traceback.print_exc()
    _cleanup()
    sys.exit(1)


# ── Layer 2: LXMF Router ─────────────────────────────────────────────

print("\n" + "=" * 60)
print("LAYER 2: LXMF Messaging")
print("=" * 60)

try:
    if LXMF is None:
        print("  ❌ LXMF module not importable")
    else:
        print(f"  ✅ LXMF module loaded (version: {getattr(LXMF, '__version__', '?')})")

        # Create LXMF router using the already-initialized Reticulum
        _lxmf_configdir = "/tmp/lmao_diag_lxmf"
        _lxmf_router = LXMF.LXMRouter(
            identity=_rns_identity,
            storagepath=_lxmf_configdir,
        )
        # Register delivery identity (required for receiving messages)
        _lxmf_router.register_delivery_identity(_rns_identity, display_name="lmao-diag")
        print(f"  ✅ LXMF router created")
        print(f"  ✅ Identity: {RNS.hexrep(_rns_identity.hash, delimit=False)}")

        # Test sending a message to self (loopback)
        print(f"\n  → Testing message loopback...")
        received = []

        def test_callback(message):
            received.append(message)
            source = message.get_source()
            source_hash = RNS.hexrep(source.hash, delimit=False) if source else "?"
            content = getattr(message, 'content', b'')[:50]
            print(f"     ✅ Message received!")
            print(f"        From: {source_hash}")
            print(f"        Content: {content}")

        _lxmf_router.register_delivery_callback(test_callback)

        # Send a test message to self — must wrap identity in Destination
        test_msg = LXMF.LXMessage(
            destination=_identity_to_destination(_rns_identity),
            source=_identity_to_destination(_rns_identity),
            content=b"Hello from diagnostics!",
            title="p:Test",
            desired_method=LXMF.LXMessage.OPPORTUNISTIC,
        )
        _lxmf_router.handle_outbound(test_msg)
        print(f"  ✅ Test message sent to self")

        # Give it a moment to be delivered
        time.sleep(1)

        if received:
            print(f"  ✅ Loopback working — {len(received)} message(s) received")
        else:
            print(f"  ⚠️  No loopback received (expected for opportunistic delivery without announce)")

except Exception as e:
    print(f"  ❌ LXMF test failed: {e}")
    import traceback
    traceback.print_exc()


# ── Layer 3: Protobuf Application ────────────────────────────────────

print("\n" + "=" * 60)
print("LAYER 3: Protobuf Application Layer")
print("=" * 60)

try:
    from lma_core import LMAOEnvelope, SensorReport, SensorReading, TextMessage
    from lma_core.message_utils import decode_lmao_message
    from google.protobuf.message import DecodeError

    print("  ✅ Protobuf stubs imported successfully")

    # Test 1: Encode a TextMessage
    envelope = LMAOEnvelope()
    envelope.text.node_id = "deadbeefdeadbeef"
    envelope.text.content = "Hello from diagnostic test!"
    envelope.text.timestamp = int(time.time() * 1000)

    encoded = envelope.SerializeToString()
    print(f"  ✅ TextMessage encoded: {len(encoded)} bytes")
    print(f"     Hex: {encoded.hex()}")

    # Test 2: Decode it back
    decoded = decode_lmao_message(encoded)
    print(f"  ✅ TextMessage decoded: \"{decoded}\"")

    # Test 3: Encode a SensorReport
    envelope2 = LMAOEnvelope()
    envelope2.sensor.node_id = "cafebabe"
    envelope2.sensor.seq = 42
    envelope2.sensor.battery = 3.7
    reading = envelope2.sensor.readings.add()
    reading.sensor_id = 1
    reading.value = 23.5  # temperature
    reading2 = envelope2.sensor.readings.add()
    reading2.sensor_id = 2
    reading2.value = 55.0  # humidity

    encoded2 = envelope2.SerializeToString()
    print(f"  ✅ SensorReport encoded: {len(encoded2)} bytes (would fit in LoRa packet)")
    print(f"     Readings: temp={envelope2.sensor.readings[0].value}°C, "
          f"humidity={envelope2.sensor.readings[1].value}%")
    print(f"     Wire size: {len(encoded2)} bytes (vs ~71 B with msgpack)")

    # Test 4: Decode SensorReport via decode_lmao_message (should fall through)
    result = decode_lmao_message(encoded2)
    print(f"  ✅ SensorReport decode result: \"{result}\"")
    # Note: decode_lmao_message falls back to raw text for non-text envelopes

    # Test 5: Cross-validate with the Cardputer encoder
    try:
        # The root "proto" module was already loaded when lma_core was imported,
        # so we must evict it from sys.modules to allow the cardputer_client
        # proto package to be found instead.
        for _mod in list(sys.modules):
            if _mod == "proto" or _mod.startswith("proto."):
                del sys.modules[_mod]
        cardputer_path = os.path.join(os.path.dirname(__file__), "cardputer_client")
        if cardputer_path in sys.path:
            sys.path.remove(cardputer_path)
        sys.path.insert(0, cardputer_path)
        from proto.lma_encoder import (
            decode_envelope,
            encode_sensor_envelope,
            encode_sensor_report,
            make_poc_message,
            parse_poc_message,
        )

        # Encode with the MicroPython encoder
        mp_encoded = encode_sensor_envelope(
            node_id="cafebabe",
            seq=42,
            battery=3.7,
            readings=[
                {"sensor_id": 1, "value": 23.5, "unit": "", "timestamp_ms": 0},
                {"sensor_id": 2, "value": 55.0, "unit": "", "timestamp_ms": 0},
            ],
        )
        print(f"  ✅ Cardputer encoder: {len(mp_encoded)} bytes")
        print(f"     Hex: {mp_encoded.hex()}")

        # Decode the protobuf version with the MicroPython decoder
        mp_decoded = decode_envelope(encoded2)
        print(f"  ✅ Cardputer decoder parsed protobuf envelope: {mp_decoded}")

        # Bidirectional decode check: both produce semantically identical protobuf
        env_a = LMAOEnvelope()
        env_a.ParseFromString(encoded2)  # protobuf library → protobuf library
        env_b = LMAOEnvelope()
        env_b.ParseFromString(mp_encoded)  # Cardputer encoder → protobuf library
        cardputer_can_read_host = decode_envelope(encoded2) is not None
        semantic_match = (
            env_a.sensor.node_id == env_b.sensor.node_id
            and env_a.sensor.seq == env_b.sensor.seq
            and abs(env_a.sensor.readings[0].value - env_b.sensor.readings[0].value) < 0.01
        )
        if semantic_match:
            print(f"  ✅ Encoder cross-validation: semantic MATCH")
            if len(encoded2) != len(mp_encoded):
                print(f"     (wire sizes differ: host={len(encoded2)}B vs cardputer={len(mp_encoded)}B —")
                print(f"      protobuf library omits default values; Cardputer encoder includes them)")
        else:
            print(f"  ❌ Encoder cross-validation: semantic MISMATCH")
            print(f"     Host: node_id={env_a.sensor.node_id} seq={env_a.sensor.seq} temp={env_a.sensor.readings[0].value}")
            print(f"     Cardputer: node_id={env_b.sensor.node_id} seq={env_b.sensor.seq} temp={env_b.sensor.readings[0].value}")

    except ImportError as e:
        print(f"  ⚠️  Cardputer encoder test skipped: {e}")
    except Exception as e:
        print(f"  ⚠️  Cardputer encoder error: {e}")

    # ── Test 6: Full Loop — Cardputer → Server → Cardputer ────────────────
    print(f"\n  {'─' * 50}")
    print(f"  → Full Loop: Cardputer → Server → Cardputer...")
    try:
        # Re-import Cardputer encoder (already loaded above; re-path if needed)
        for _mod in list(sys.modules):
            if _mod == "proto" or _mod.startswith("proto."):
                del sys.modules[_mod]
        if cardputer_path not in sys.path:
            sys.path.insert(0, cardputer_path)
        from proto.lma_encoder import (
            decode_envelope,
            encode_sensor_envelope,
            make_poc_message,
            parse_poc_message,
        )

        # Step 1: Cardputer sends a SensorReport (IoT message)
        cardputer_node_id = "cafebabe"
        cardputer_sensor_bytes = encode_sensor_envelope(
            node_id=cardputer_node_id,
            seq=42,
            battery=3.7,
            readings=[
                {"sensor_id": 1, "value": 23.5, "unit": "C", "timestamp_ms": 0},
                {"sensor_id": 2, "value": 55.0, "unit": "%", "timestamp_ms": 0},
            ],
        )
        print(f"  \n  Step 1 — Cardputer encodes SensorReport:")
        print(f"     ✅ {len(cardputer_sensor_bytes)} bytes (ready for LXMF Content)")

        # Step 2: Server receives and decodes the SensorReport
        server_envelope = LMAOEnvelope()
        server_envelope.ParseFromString(cardputer_sensor_bytes)
        sensor_fields = [
            f"sensor_id={r.sensor_id}, value={r.value}{r.unit}"
            for r in server_envelope.sensor.readings
        ]
        print(f"  Step 2 — Server decodes SensorReport:")
        print(f"     ✅ node_id={server_envelope.sensor.node_id}")
        print(f"        seq={server_envelope.sensor.seq}")
        print(f"        battery={server_envelope.sensor.battery}V")
        print(f"        readings: {', '.join(sensor_fields)}")

        # Step 3: Server builds and sends a protobuf ACK TextMessage reply
        ack_text = f"ACK from LMAO Server — received SensorReport ({len(cardputer_sensor_bytes)} bytes)"
        reply_envelope = LMAOEnvelope()
        reply_envelope.text.node_id = "lmao-server"
        reply_envelope.text.content = ack_text
        reply_envelope.text.timestamp = int(time.time() * 1000)
        server_ack_bytes = reply_envelope.SerializeToString()
        print(f"  Step 3 — Server builds ACK TextMessage:")
        print(f"     ✅ ACK: {ack_text!r}")
        print(f"     ✅ {len(server_ack_bytes)} bytes protobuf-encoded")

        # Step 4: Cardputer receives and decodes the ACK
        cardputer_decoded = parse_poc_message(server_ack_bytes)
        print(f"  Step 4 — Cardputer decodes ACK:")
        if cardputer_decoded:
            print(f"     ✅ Decoded: {cardputer_decoded!r}")
            if cardputer_decoded == ack_text:
                print(f"     ✅ Round-trip MATCH — text intact through encode→send→decode")
            else:
                print(f"     ❌ Round-trip MISMATCH — expected: {ack_text!r}")
        else:
            print(f"     ❌ parse_poc_message returned None — ACK not decodable")

        # Step 5: Cardputer also sends TextMessage (POC hello), verify server decodes it
        # First, restore host proto path (remove Cardputer path, re-import host proto)
        if cardputer_path in sys.path:
            sys.path.remove(cardputer_path)
        for _mod in list(sys.modules):
            if _mod == "proto" or _mod.startswith("proto."):
                del sys.modules[_mod]
        # Force re-import of host proto via lma_core
        from lma_core import LMAOEnvelope  # noqa: F811, F401

        hello_text = "Hello from Cardputer — seq 1"
        cardputer_hello_bytes = make_poc_message(cardputer_node_id, hello_text)
        server_hello = decode_lmao_message(cardputer_hello_bytes)
        print(f"  \n  Step 5 — Cardputer TextMessage loop:")
        print(f"     Sent: {hello_text!r}")
        print(f"     Server decoded: {server_hello!r}")
        if server_hello == hello_text:
            print(f"     ✅ TextMessage round-trip MATCH")
        else:
            print(f"     ❌ TextMessage round-trip MISMATCH")

        print(f"  \n  {'✅' if cardputer_decoded == ack_text and server_hello == hello_text else '❌'} Full loop: COMPLETE")

    except ImportError as e:
        print(f"  ⚠️  Full loop test skipped: {e}")
    except Exception as e:
        print(f"  ⚠️  Full loop test error: {e}")
        import traceback
        traceback.print_exc()

    # Re-import proto module for the host (cleared for Cardputer above)
    for _mod in list(sys.modules):
        if _mod == "proto" or _mod.startswith("proto."):
            del sys.modules[_mod]
    from lma_core import LMAOEnvelope, SensorReport, SensorReading, TextMessage

    print()  # spacing before summary

except ImportError as e:
    print(f"  ❌ Protobuf import error: {e}")
except Exception as e:
    print(f"  ❌ Protobuf test failed: {e}")
    import traceback
    traceback.print_exc()


# ── Cleanup ──────────────────────────────────────────────────────────

_cleanup()


# ── Summary ──────────────────────────────────────────────────────────

print("\n" + "=" * 60)
print("DIAGNOSTIC SUMMARY")
print("=" * 60)
print("To test the full LoRa path (requires Cardputer + RNode):")
print("  bazel test //tests:test_cardputer_lora_e2e --test_output=all")
print()
print("To run the server manually:")
print("  LMAO_RNODE_PORT=/dev/ttyUSB0 bazel run //lmao_server:server")
print()
print("To run the human client:")
print("  LMAO_RNODE_PORT=/dev/ttyUSB0 bazel run //human_client:client")