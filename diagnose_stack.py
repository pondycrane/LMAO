#!/usr/bin/env python3
"""
Diagnose each layer of the LMAO communication stack.
Tests: RNode hardware → Reticulum → LXMF → Protobuf application
"""

import os
import sys
import time
import tempfile
import shutil
import struct

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

# ── Layer 1: Reticulum Initialization ────────────────────────────────

print("\n" + "=" * 60)
print("LAYER 1: Reticulum Network Stack")
print("=" * 60)

# Use the project's own config builder
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from lma_core.config_utils import RnsConfig
    from lma_core.rns_di import RNS, LXMF

    if RNS is None:
        print("  ❌ RNS module not importable (not installed)")
    else:
        print(f"  ✅ RNS module loaded (version: {getattr(RNS, '__version__', '?')})")

        # Build config using the project's helper
        cfg = RnsConfig(transport_path="/tmp/lmao_diag_rns")
        configdir = cfg.get_configdir()
        print(f"  ✅ Config directory created at {configdir}")

        # Show the config content
        with open(os.path.join(configdir, "config")) as f:
            config_content = f.read()
        print(f"\n  Config content:\n{config_content}\n")

        # Try to initialize Reticulum
        try:
            # We need to set up the config dir first
            rns_instance = RNS.Reticulum(configdir=configdir)
            print(f"  ✅ Reticulum initialized successfully")

            # Check interfaces
            rns_inst = RNS.Reticulum.get_instance()
            # Interfaces are stored in a list; check the attribute
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

            # Check if we have a valid identity
            identity = RNS.Identity()
            print(f"  ✅ Identity created: {RNS.hexrep(identity.hash, delimit=False)}")

        except Exception as e:
            print(f"  ❌ Reticulum init failed: {e}")
            import traceback
            traceback.print_exc()
        finally:
            # Cleanup - teardown Reticulum for next test
            try:
                RNS.Reticulum.get_instance().teardown()
            except:
                pass
            shutil.rmtree(configdir, ignore_errors=True)
            # Also clear the singleton so next test can reinit
            if hasattr(RNS.Reticulum, '_Reticulum__instance'):
                RNS.Reticulum._Reticulum__instance = None

except ImportError as e:
    print(f"  ❌ Import error: {e}")

# ── Layer 2: LXMF Router ─────────────────────────────────────────────

print("\n" + "=" * 60)
print("LAYER 2: LXMF Messaging")
print("=" * 60)

try:
    if LXMF is None:
        print("  ❌ LXMF module not importable")
    else:
        print(f"  ✅ LXMF module loaded (version: {getattr(LXMF, '__version__', '?')})")

        # Initialize Reticulum + LXMF using the shared helper
        from lma_core.rns_init import init_rns_and_lxmf

        # Temporarily redirect for clean test
        test_cfg = RnsConfig(transport_path="/tmp/lmao_diag_lxmf")
        test_configdir = test_cfg.get_configdir()

        # Check RNode availability
        rnode_port = test_cfg._RNODE_PORT
        rnode_exists = os.path.exists(rnode_port)
        print(f"  {'✅' if rnode_exists else '⚠️'}  RNode port {rnode_port}: {'found' if rnode_exists else 'NOT FOUND'}")

        identity, router = init_rns_and_lxmf(
            rnode_port=rnode_port,
            configdir_factory=test_cfg.get_configdir,
            identity_storage_path="/tmp/lmao_diag_identity",
            rnode_exists=rnode_exists,
        )
        print(f"  ✅ LXMF router created")
        print(f"  ✅ Identity: {RNS.hexrep(identity.hash, delimit=False)}")

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

        router.register_delivery_callback(test_callback)

        # Send a test message to self
        test_msg = LXMF.LXMessage(
            destination=identity,
            source=identity,
            content=b"Hello from diagnostics!",
            title="p:Test",
            desired_method=LXMF.LXMessage.OPPORTUNISTIC,
        )
        router.handle_outbound(test_msg)
        print(f"  ✅ Test message sent to self")

        # Give it a moment to be delivered
        time.sleep(1)

        if received:
            print(f"  ✅ Loopback working — {len(received)} message(s) received")
        else:
            print(f"  ⚠️  No loopback received (expected for opportunistic delivery without announce)")

        # Cleanup
        router.shutdown()
        shutil.rmtree(test_configdir, ignore_errors=True)
        shutil.rmtree("/tmp/lmao_diag_identity", ignore_errors=True)
        # Reset singleton
        if hasattr(RNS.Reticulum, '_Reticulum__instance'):
            RNS.Reticulum._Reticulum__instance = None

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
    import time as time_module

    print("  ✅ Protobuf stubs imported successfully")

    # Test 1: Encode a TextMessage
    envelope = LMAOEnvelope()
    envelope.text.node_id = "deadbeefdeadbeef"
    envelope.text.content = "Hello from diagnostic test!"
    envelope.text.timestamp = int(time_module.time() * 1000)

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
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "cardputer_client"))
        from proto.lma_encoder import encode_sensor_report, decode_envelope

        # Encode with the MicroPython encoder
        mp_encoded = encode_sensor_report(
            node_id="cafebabe",
            seq=42,
            battery=3.7,
            readings=[(1, 23.5), (2, 55.0)],
        )
        print(f"  ✅ Cardputer encoder: {len(mp_encoded)} bytes")
        print(f"     Hex: {mp_encoded.hex()}")

        # Decode the protobuf version with the MicroPython decoder
        import binascii
        mp_decoded = decode_envelope(encoded2)
        print(f"  ✅ Cardputer decoder parsed protobuf envelope: {mp_decoded}")

        # Compare: both should produce valid protobuf
        env_a = LMAOEnvelope()
        env_a.ParseFromString(encoded2)
        env_b = LMAOEnvelope()
        env_b.ParseFromString(mp_encoded)
        match = (
            env_a.sensor.node_id == env_b.sensor.node_id
            and env_a.sensor.seq == env_b.sensor.seq
            and abs(env_a.sensor.readings[0].value - env_b.sensor.readings[0].value) < 0.01
        )
        print(f"  ✅ {'✅' if match else '❌'} Encoder cross-validation: {'MATCH' if match else 'MISMATCH'}")

    except ImportError as e:
        print(f"  ⚠️  Cardputer encoder test skipped: {e}")
    except Exception as e:
        print(f"  ⚠️  Cardputer encoder error: {e}")

except ImportError as e:
    print(f"  ❌ Protobuf import error: {e}")
except Exception as e:
    print(f"  ❌ Protobuf test failed: {e}")
    import traceback
    traceback.print_exc()

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