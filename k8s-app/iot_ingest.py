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
  python k8s-app/iot_ingest.py [--server SERVER] [--send] [--subscribe] [--subscribe-timeout SECONDS] [--get-identity] [--use-nats] [--store] [--db-path PATH] [--query SQL]

Environment Variables:
  LMAO_SERVER  gRPC target (default: lmao-server.default.svc.cluster.local:50051)
  NATS_SERVER  NATS target (default: nats://nats-server.default.svc.cluster.local:4222)
"""

import argparse
import asyncio
import logging
import os
import sys
import time

logger = logging.getLogger(__name__)

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
    from lma_core import LMAOEnvelope
    from lma_core.grpc_types import (
        SendRequest,
        SubscribeRequest,
        GetIdentityRequest,
        LMAOStub,
        LMAOServicer,
    )
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    try:
        from lma_core import LMAOEnvelope
        from lma_core.grpc_types import (
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
    """Build a serialized LMAOEnvelope containing a SensorReport.

    Convention: *node_id* SHOULD be the device's Reticulum identity hex hash
    (32 hex characters).  This guarantees global uniqueness and cryptographic
    attribution since the LXMF ``source_hash`` already carries the same
    identity.  Human-readable names are acceptable for testing but should
    be replaced with identity hashes in production.
    """
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
    payload = build_sensor_envelope("a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6", 22.5, 68.0)
    request = SendRequest(envelope=payload)
    response = stub.Send(request)
    print(f"Send response: status={response.status}, dest={response.destination_hash}")
    print()


async def send_example_nats(nats_server: str, subject: str = "lmao.messages.env"):
    """Publish a sensor reading to NATS JetStream."""
    print("=== Send Example (NATS) ===")
    from lma_core.queue import NatsQueue

    payload = build_sensor_envelope("a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6", 22.5, 68.0)
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
            logger.info("gRPC subscribe stream cancelled")
        elif e.code() == grpc.StatusCode.UNAVAILABLE:
            print(f"  Subscribe error: server unavailable — {e.details()}")
            logger.warning("gRPC subscribe failed (UNAVAILABLE): %s", e.details())
        elif e.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
            print(f"  Subscribe timeout: {e.details()}")
            logger.warning("gRPC subscribe timeout (DEADLINE_EXCEEDED): %s", e.details())
        else:
            print(f"  Subscribe error: code={e.code()} details={e.details()}")
            logger.warning("gRPC subscribe error: code=%s details=%s", e.code(), e.details())
    print()


async def subscribe_example_nats(
    nats_server: str,
    subject: str = "lmao.messages.>",
    timeout: int = 5,
    store_path: str | None = None,
):
    """Consume messages from NATS JetStream via a durable pull consumer.

    If *store_path* is provided, each message is also persisted to a
    DuckDB database at that path via ``DuckDbStore.store_sensor_report()``.
    """
    print(f"=== Subscribe Example (NATS, listening for {timeout}s) ===")
    from lma_core.queue import NatsQueue

    received: list = []

    store = None
    if store_path:
        from lma_core.storage import DuckDbStore

        store = DuckDbStore(name="iot-ingest-subscriber")
        store.initialize(store_path)
        print(f"  DuckDB store initialized at {store_path}")

    def _on_message(msg):
        print(f"  Received {len(msg.data)} bytes on '{msg.subject}'")
        received.append(msg)

    async def _store_and_ack(msg):
        """Thin wrapper that stores the message if store is active, then ACKs."""
        if store is not None:
            try:
                await store.store_sensor_report(bytes(msg.data))
            except Exception:
                logger.warning(
                    "Failed to persist message on '%s' — NAK-ing",
                    msg.subject,
                    exc_info=True,
                )
                raise  # Trigger NAK in the subscribe loop
        # If no store, _on_message handles the print — still call it
        _on_message(msg)

    nq = NatsQueue(name="iot-ingest-subscriber")
    try:
        await nq.connect(servers=nats_server)
        await nq.ensure_stream("LMAO_MESSAGES", [subject])

        # Subscribe as a background task, cancel after timeout
        task = asyncio.ensure_future(
            nq.subscribe(subject, "iot-ingest", _store_and_ack)
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
        if store is not None:
            store.close()
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
    parser.add_argument(
        "--store",
        action="store_true",
        help="Persist consumed messages to DuckDB (requires --subscribe --use-nats)",
    )
    parser.add_argument(
        "--db-path",
        default=os.environ.get("DUCKDB_PATH", "/data/sensors.db"),
        help="Path to DuckDB database file (default: /data/sensors.db or $DUCKDB_PATH)",
    )
    parser.add_argument(
        "--query",
        type=str,
        default=None,
        help="Run a SQL query against the DuckDB store and print results",
    )
    args = parser.parse_args()

    # ── Query-only mode (DuckDB read, no NATS) ──────────────────
    if args.query and not (args.send or args.subscribe):
        try:
            from lma_core.storage import DuckDbStore
        except ImportError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)

        try:
            store = DuckDbStore(name="iot-ingest-query")
        except ImportError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)

        try:
            store.initialize(args.db_path, read_only=True)
        except Exception as e:
            print(f"ERROR: Could not open DuckDB database at {args.db_path}: {e}", file=sys.stderr)
            sys.exit(1)

        try:
            rows = asyncio.run(store.query(args.query))
            if rows:
                # Fetch column names for a header row
                try:
                    col_rows = asyncio.run(
                        store.query(
                            f"SELECT column_name FROM information_schema.columns "
                            f"WHERE table_name = 'sensor_readings' "
                            f"ORDER BY ordinal_position"
                        )
                    )
                    cols = [r[0] for r in col_rows]
                    if cols:
                        print("  ".join(cols))
                        print("-" * 60)
                except Exception:
                    logger.debug(
                        "Could not fetch column names for query output",
                        exc_info=True,
                    )
                for row in rows:
                    print(row)
            else:
                print("(no rows returned)")
        finally:
            store.close()
        print("Done.")
        return

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
                    args.nats_server,
                    timeout=args.subscribe_timeout,
                    store_path=args.db_path if args.store else None,
                )

        try:
            asyncio.run(_nats_main())
        except ImportError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        except Exception:
            logger.exception("NATS operation failed")
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
    except Exception:
        logger.exception("Unhandled error in main()")
        sys.exit(1)
