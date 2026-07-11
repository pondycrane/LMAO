"""Persistent IoT Ingest Consumer — continuously subscribes to NATS
JetStream and persists all sensor data to DuckDB.

This service is designed to run as a long-lived K8s Deployment.
It accepts configuration via environment variables and handles
SIGTERM/SIGINT for graceful shutdown.

Configuration (environment variables):
    NATS_SERVER    NATS server URL (default: nats://localhost:4222)
    DUCKDB_PATH    Path to DuckDB database file (default: /data/sensors.db)
    CONSUMER_NAME  Durable consumer name (default: iot-ingest)

Usage::

    python k8s-app/iot_ingest_consumer.py

    NATS_SERVER=nats://nats:4222 DUCKDB_PATH=/data/sensors.db \\
        python k8s-app/iot_ingest_consumer.py
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_NATS_SERVER = "nats://localhost:4222"
_DEFAULT_DUCKDB_PATH = "/data/sensors.db"
_DEFAULT_CONSUMER_NAME = "iot-ingest"
_STREAM_NAME = "LMAO_MESSAGES"
_STREAM_SUBJECTS = ["lmao.messages.>"]


async def _store_and_ack(msg, store):
    """Callback invoked on each NATS message: persist to DuckDB, then ACK.

    On failure, raises to trigger NAK in the subscribe loop so the
    message is redelivered.
    """
    await store.store_sensor_report(bytes(msg.data))


async def main(shutdown_event: asyncio.Event | None = None) -> None:
    """Entry point for the persistent iot-ingest consumer.

    Reads configuration from environment variables, connects to NATS,
    initializes DuckDB, and runs the subscribe loop until cancelled.
    On shutdown, gracefully closes both NATS and DuckDB.

    Args:
        shutdown_event: Optional pre-constructed event for testing.
            When provided, the function uses this event instead of
            creating its own. Tests can inject a pre-set event so
            ``main()`` exits immediately after subscribe returns.
    """
    nats_server = os.environ.get("NATS_SERVER", _DEFAULT_NATS_SERVER)
    duckdb_path = os.environ.get("DUCKDB_PATH", _DEFAULT_DUCKDB_PATH)
    consumer_name = os.environ.get("CONSUMER_NAME", _DEFAULT_CONSUMER_NAME)

    nq = None
    store = None

    # Signal handlers
    if shutdown_event is None:
        shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        _logger.info("Received shutdown signal — draining...")
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            _logger.warning(
                "Signal handlers not supported on this platform — "
                "consumer may not shut down gracefully on SIGTERM/SIGINT. "
                "This is expected in test environments but should not "
                "happen in production (Linux/K8s) containers."
            )

    try:
        from lma_core.queue import NatsQueue
        from lma_core.storage import DuckDbStore

        nq = NatsQueue(name=consumer_name)
        store = DuckDbStore(name=consumer_name)

        _logger.info(
            "IoT Ingest Consumer starting: NATS=%s, DuckDB=%s, name=%s",
            nats_server,
            duckdb_path,
            consumer_name,
        )

        await nq.connect(servers=nats_server)
        await nq.ensure_stream(_STREAM_NAME, _STREAM_SUBJECTS)
        store.initialize(duckdb_path)

        _logger.info("IoT Ingest Consumer started — listening on %s", _STREAM_SUBJECTS)

        # Create a callback with the store bound
        async def _callback(msg):
            await _store_and_ack(msg, store)

        # Subscribe as a background task
        subscribe_task = asyncio.ensure_future(
            nq.subscribe("lmao.messages.>", consumer_name, _callback)
        )

        # Wait for shutdown signal
        await shutdown_event.wait()

        # Cancel the subscribe task
        subscribe_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await subscribe_task

        _logger.info("IoT Ingest Consumer shutting down...")

    except ImportError as exc:
        _logger.critical("Missing dependency: %s", exc)
        sys.exit(1)
    except Exception:
        _logger.critical("Fatal error in consumer", exc_info=True)
        sys.exit(1)
    finally:
        if nq is not None:
            await nq.close()
        if store is not None:
            store.close()
        _logger.info("IoT Ingest Consumer stopped.")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    try:
        asyncio.run(main())
    except ImportError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception:
        _logger.exception("Consumer failed")
        sys.exit(1)
