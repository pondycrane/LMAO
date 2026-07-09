"""
IoT Ingest — Example K8s pod that communicates with LMAO Server via gRPC.

This script demonstrates how to use the LMAO gRPC API from within a K8s pod:

  1. Send:   Inject a SensorReport into the LoRa mesh
  2. Subscribe: Stream incoming LXMF messages
  3. GetIdentity: Read the server's Reticulum identity

Usage:
  python k8s-app/iot_ingest.py [--server SERVER] [--send] [--subscribe] [--subscribe-timeout SECONDS] [--get-identity]

Environment Variables:
  LMAO_SERVER  gRPC target (default: lmao-server.default.svc.cluster.local:50051)
"""

import argparse
import os
import time
import sys

try:
    import grpc
except ImportError:
    print("ERROR: grpcio is required. Install with: pip install grpcio grpcio-tools",
          file=sys.stderr)
    sys.exit(1)

# Use lma_core to import proto stubs (single import point)
try:
    from lma_core import (
        LMAOEnvelope,
        SendRequest,
        SubscribeRequest,
        GetIdentityRequest,
        LMAOStub,
        LMAOServicer,
    )
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    try:
        from lma_core import (
            LMAOEnvelope,
            SendRequest,
            SubscribeRequest,
            GetIdentityRequest,
            LMAOStub,
            LMAOServicer,  # noqa: F401
        )
    except ImportError:
        print("ERROR: Cannot import gRPC stubs. Run from repo root or set PYTHONPATH.",
              file=sys.stderr)
        sys.exit(1)


def build_sensor_envelope(node_id: str, temperature: float, humidity: float) -> bytes:
    """Build a serialized LMAOEnvelope containing a SensorReport."""
    envelope = LMAOEnvelope()
    envelope.sensor.node_id = node_id
    envelope.sensor.seq = int(time.time())
    envelope.sensor.battery = 3.7

    reading_temp = envelope.sensor.readings.add()
    reading_temp.sensor_id = 1
    reading_temp.value = temperature
    reading_temp.unit = "C"
    reading_temp.timestamp_ms = int(time.time() * 1000)

    reading_hum = envelope.sensor.readings.add()
    reading_hum.sensor_id = 2
    reading_hum.value = humidity
    reading_hum.unit = "%"
    reading_hum.timestamp_ms = int(time.time() * 1000)

    return envelope.SerializeToString()


def send_example(stub: LMAOStub):
    """Send a sensor reading via gRPC."""
    print("=== Send Example ===")
    payload = build_sensor_envelope("k8s-sensor-01", 22.5, 68.0)
    request = SendRequest(envelope=payload)
    response = stub.Send(request)
    print(f"Send response: status={response.status}, dest={response.destination_hash}")
    print()


def subscribe_example(stub: LMAOStub, timeout: int = 5):
    """Subscribe to incoming messages for 'timeout' seconds."""
    print(f"=== Subscribe Example (listening for {timeout}s) ===")
    request = SubscribeRequest(title_filter="")
    try:
        for msg in stub.Subscribe(request, timeout=timeout):
            src = msg.source_hash or "<unknown>"
            print(f"  Received {len(msg.envelope)} bytes from {src}")
    except grpc.RpcError as e:
        if e.code() == grpc.StatusCode.CANCELLED:
            print("Subscribe stream ended (CANCELLED)")
        else:
            print(f"  Subscribe error: code={e.code()} details={e.details()}")
    print()


def get_identity_example(stub: LMAOStub):
    """Fetch the server's identity."""
    print("=== GetIdentity Example ===")
    response = stub.GetIdentity(GetIdentityRequest())
    print(f"Server identity: {response.identity_hex}")
    print(f"Node name:       {response.node_name}")
    print()


def main():
    parser = argparse.ArgumentParser(description="LMAO gRPC IoT Ingest Example")
    parser.add_argument(
        "--server",
        default=os.environ.get("LMAO_SERVER", "localhost:50051"),
        help="gRPC server address",
    )
    parser.add_argument("--send", action="store_true", help="Run Send example")
    parser.add_argument(
        "--subscribe", action="store_true", help="Run Subscribe example"
    )
    parser.add_argument(
        "--get-identity", action="store_true", help="Run GetIdentity example"
    )
    parser.add_argument(
        "--subscribe-timeout",
        type=int,
        default=5,
        help="Seconds to listen on subscribe stream (default: 5)",
    )
    args = parser.parse_args()

    if not (args.send or args.subscribe or args.get_identity):
        # Default: run all examples
        args.send = True
        args.subscribe = True
        args.get_identity = True

    channel = grpc.insecure_channel(args.server)
    stub = LMAOStub(channel)

    print(f"Connected to LMAO server at {args.server}")
    print()

    if args.get_identity:
        get_identity_example(stub)
    if args.send:
        send_example(stub)
    if args.subscribe:
        subscribe_example(stub, timeout=args.subscribe_timeout)

    channel.close()
    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except grpc.RpcError as e:
        print(f"ERROR: gRPC call failed: {e.code()} - {e.details()}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
