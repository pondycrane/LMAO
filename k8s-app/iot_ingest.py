"""
IoT Ingest — Example K8s pod that communicates with LMAO Server via gRPC.

This script demonstrates how to use the LMAO gRPC API from within a K8s pod:

  1. Send:   Inject a SensorReport into the LoRa mesh
  2. Subscribe: Stream incoming LXMF messages
  3. GetIdentity: Read the server's Reticulum identity

When ``--use-nats`` is set, the script publishes and consumes messages
through the in-cluster NATS JetStream queue instead of gRPC, providing
durable, at-least-once delivery with queue-group load balancing.

Usage:
  python k8s-app/iot_ingest.py [--server SERVER] [--send] [--subscribe] [--subscribe-timeout SECONDS] [--get-identity] [--use-nats]

Environment Variables:
  LMAO_SERVER  gRPC target (default: lmao-server.default.svc.cluster.local:50051)
  NATS_SERVER  NATS target (default: nats://nats-server.default.svc.cluster.local:4222)
"""

import argparse
import asyncio
import os
import time
import sys

# ---------------------------------------------------------------------------
# gRPC imports (required for default mode)
# ---------------------------------------------------------------------------

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
    print("=== Send Example (gRPC) ===")
    payload = build_sensor_envelope("k8s-sensor-01", 22.5, 68.0)
    request = SendRequest(envelope=payload)
    response = stub.Send(request)
    print(f"Send response: status={response.status}, dest={response.destination_hash}")
    print()


async def send_example_nats(nats_server: str, subject: str = "lmao.messages.env"):
    """Publish a sensor reading to NATS JetStream."""
    print("=== Send Example (NATS) ===")
    from lma_core.queue import NatsQueue

    payload = build_sensor_envelope("k8s-sensor-01", 22.5, 68.0)
    nq = NatsQueue(name="iot-ingest-sender")
    try:
        await nq.connect(servers=nats_server)
        # Stream subject filter uses wildcard, publish subject is concrete —
        # both must align with the subscribe wildcard "lmao.messages.>"
        await nq.ensure_stream("LMAO_MESSAGES", ["lmao.messages.>"])
        ack = await nq.publish(subject, payload)
        print(f"Published {len(payload)} bytes to '{subject}' (seq={ack.seq})")
    finally:
        await nq.close()
    print()


def subscribe_example(stub: LMAOStub, timeout: int = 5):
    """Subscribe to incoming messages via gRPC for 'timeout' seconds."""
    print(f"=== Subscribe Example (gRPC, listening for {timeout}s) ===")
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


async def subscribe_example_nats(
    nats_server: str,
    subject: str = "lmao.messages.>",
    timeout: int = 5,
):
    """Consume messages from NATS JetStream via a durable pull consumer."""
    print(f"=== Subscribe Example (NATS, listening for {timeout}s) ===")
    from lma_core.queue import NatsQueue

    received: list = []

    def _on_message(msg):
        print(f"  Received {len(msg.data)} bytes on '{msg.subject}'")
        received.append(msg)

    nq = NatsQueue(name="iot-ingest-subscriber")
    try:
        await nq.connect(servers=nats_server)
        await nq.ensure_stream("LMAO_MESSAGES", [subject])

        # Subscribe as a background task, cancel after timeout
        task = asyncio.ensure_future(
            nq.subscribe(subject, "iot-ingest", _on_message)
        )
        await asyncio.sleep(timeout)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        print(f"  Total received: {len(received)} message(s)")
    finally:
        await nq.close()
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
    parser.add_argument(
        "--nats-server",
        default=os.environ.get(
            "NATS_SERVER", "nats://nats-server.default.svc.cluster.local:4222"
        ),
        help="NATS server address (used when --use-nats is set)",
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
    parser.add_argument(
        "--use-nats",
        action="store_true",
        help="Use NATS JetStream queue instead of gRPC for send/subscribe",
    )
    args = parser.parse_args()

    if not (args.send or args.subscribe or args.get_identity):
        # Default: run all examples
        args.send = True
        args.subscribe = True
        args.get_identity = True

    # ── NATS path ───────────────────────────────────────────────────
    if args.use_nats:
        # get_identity has no NATS equivalent — log a note
        if args.get_identity:
            print(
                "Note: GetIdentity is a gRPC-only operation. "
                "Connect to the LMAO server directly for identity info.",
                file=sys.stderr,
            )

        async def _nats_main():
            if args.send:
                await send_example_nats(args.nats_server)
            if args.subscribe:
                await subscribe_example_nats(
                    args.nats_server, timeout=args.subscribe_timeout
                )

        try:
            asyncio.run(_nats_main())
        except ImportError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"ERROR: NATS operation failed: {e}", file=sys.stderr)
            sys.exit(1)

        print("Done.")
        return

    # ── gRPC path (default) ─────────────────────────────────────────
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
