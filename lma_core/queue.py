"""NATS JetStream queue/publish-subscribe wrapper for LMAO K8s pods.

Provides a lightweight ``NatsQueue`` class that mirrors the existing
``lma_core`` module conventions: lazy imports with try/except,
module-level ``_logger``, and a class-based API with an optional
module-level factory function.

Requires ``nats-py`` at runtime (``pip install nats-py``).
When ``nats-py`` is absent the module logs a warning and
``NatsQueue`` raises ``ImportError`` with a descriptive message.

Usage::

    import asyncio
    from lma_core.queue import NatsQueue

    async def main():
        nq = NatsQueue()
        await nq.connect()
        await nq.ensure_stream("SENSOR_READINGS", ["sensors.>"])
        await nq.publish("sensors.temp", b'{"value":22.5}')
        await nq.close()

    asyncio.run(main())
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, List, Optional

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy import of nats-py — graceful fallback when absent
# ---------------------------------------------------------------------------

_NATS_AVAILABLE = False
_NATS_IMPORT_ERROR = ""

try:
    import nats  # noqa: F401
    from nats.js import JetStreamContext  # noqa: F401

    _NATS_AVAILABLE = True
except ImportError as exc:
    _NATS_IMPORT_ERROR = (
        "nats-py is not installed. NATS queue features will be unavailable. "
        "Install with: pip install nats-py"
    )
    _logger.warning(_NATS_IMPORT_ERROR)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class NatsQueue:
    """Async NATS JetStream wrapper for publish/subscribe messaging.

    Encapsulates a NATS client connection and JetStream context,
    exposing idempotent stream management and simple
    publish / subscribe primitives that K8s pods can use to
    queue messages durably.

    All public methods are ``async`` and must be driven by an
    asyncio event loop.  Typical usage::

        nq = NatsQueue()
        await nq.connect("nats://nats-server:4222")

        # Ensure the stream exists (idempotent — safe to call repeatedly)
        await nq.ensure_stream("TELEMETRY", ["telemetry.>"])

        # Publish raw bytes
        await nq.publish("telemetry.env", envelope_bytes)

        # Subscribe with a callback
        async def handle(msg):
            print(f"Got {len(msg.data)} bytes")
        await nq.subscribe("telemetry.>", "my-pod", handle)

    Parameters
    ----------
    name:
        Optional human-readable name for this queue client,
        used as a NATS connection name for easier debugging
        (``nats server report connections``).
    max_payload:
        Maximum payload size in bytes.  Messages larger than
        this are rejected before hitting the wire.  Default
        is 1 MiB — JetStream's recommended upper bound.
    """

    # JetStream stream defaults
    _MAX_AGE_NS = 7 * 24 * 60 * 60 * 1_000_000_000  # 7 days
    _MAX_STREAM_BYTES = 1_073_741_824  # 1 GiB
    _MAX_MSG_SIZE = 1_048_576  # 1 MiB
    _REPLICAS = 1

    def __init__(
        self,
        name: str = "lmao-queue",
        max_payload: int = _MAX_MSG_SIZE,
    ) -> None:
        if not _NATS_AVAILABLE:
            raise ImportError(_NATS_IMPORT_ERROR)

        self._name = name
        self._max_payload = max_payload
        self._nc: Any = None  # nats.aio.client.Client
        self._js: Any = None  # nats.js.JetStreamContext

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(
        self,
        servers: str | List[str] = "nats://localhost:4222",
        **kwargs: Any,
    ) -> None:
        """Connect to a NATS cluster and create a JetStream context.

        Parameters
        ----------
        servers:
            Comma-separated list or sequence of NATS URLs, e.g.
            ``"nats://nats-server:4222"`` or
            ``["nats://host1:4222", "nats://host2:4222"]``.
        **kwargs:
            Passed through to ``nats.connect()`` (e.g. ``token``,
            ``user``, ``password``).
        """
        if not _NATS_AVAILABLE:
            raise ImportError(_NATS_IMPORT_ERROR)

        _logger.info("Connecting to NATS at %s ...", servers)
        try:
            self._nc = await nats.connect(
                servers=servers,
                name=self._name,
                **kwargs,
            )
            self._js = self._nc.jetstream()
        except Exception:
            _logger.critical(
                "Failed to connect to NATS at %s", servers, exc_info=True
            )
            raise
        _logger.info("Connected to NATS (JetStream available)")

    async def close(self) -> None:
        """Gracefully drain and close the NATS connection."""
        if self._nc is not None:
            _logger.info("Draining NATS connection ...")
            try:
                await self._nc.drain()
            except Exception:
                _logger.warning("Error draining NATS connection", exc_info=True)
            finally:
                self._nc = None
                self._js = None
                _logger.info("NATS connection closed.")

    # ------------------------------------------------------------------
    # Stream management
    # ------------------------------------------------------------------

    async def ensure_stream(
        self,
        name: str,
        subjects: List[str],
        **overrides: Any,
    ) -> None:
        """Idempotent stream creation / update.

        If the stream already exists its configuration is updated
        to match the supplied subjects and defaults.  A no-op when
        the existing stream config is already correct.

        Parameters
        ----------
        name:
            JetStream stream name (unique within the account).
        subjects:
            NATS subject patterns that feed into this stream
            (e.g. ``["sensors.>", "commands.>"]``).
        **overrides:
            Override default stream settings.  See
            ``nats.js.api.StreamConfig`` for the full option set.
        """
        self._check_connected()

        config = {
            "name": name,
            "subjects": subjects,
            "retention": "limits",
            "max_age": self._MAX_AGE_NS,
            "max_bytes": self._MAX_STREAM_BYTES,
            "max_msg_size": self._max_payload,
            "storage": "file",
            "num_replicas": self._REPLICAS,
            **overrides,
        }

        try:
            # add_stream raises if JetStream is not available (no --js)
            await self._js.add_stream(**config)
            _logger.info("JetStream stream '%s' created with subjects: %s", name, subjects)
        except Exception as exc:
            err_msg = str(exc).lower()
            if "already exists" in err_msg or "stream name already in use" in err_msg:
                # Stream already exists — update it
                _logger.info(
                    "Stream '%s' already exists — updating config", name
                )
                try:
                    await self._js.update_stream(**config)
                    _logger.info("JetStream stream '%s' updated", name)
                except Exception:
                    _logger.error(
                        "Failed to update stream '%s'", name, exc_info=True
                    )
                    raise
            else:
                _logger.error(
                    "Failed to create stream '%s' — unexpected error", name,
                    exc_info=True
                )
                raise

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    async def publish(
        self,
        subject: str,
        payload: bytes,
        **kwargs: Any,
    ) -> Any:
        """Publish raw bytes to a JetStream subject.

        Parameters
        ----------
        subject:
            NATS subject to publish to.
        payload:
            Raw bytes to publish (typically ``LMAOEnvelope.SerializeToString()``).
            Empty payloads are accepted.
        **kwargs:
            Passed to ``JetStreamContext.publish()`` (e.g. ``stream``,
            ``timeout``).
        Returns
        -------
            The ``PubAck`` response from the NATS server.
        Raises
        ------
        ValueError:
            If *payload* exceeds ``self._max_payload``.
        """
        self._check_connected()

        if len(payload) > self._max_payload:
            raise ValueError(
                f"Payload size {len(payload)} bytes exceeds maximum "
                f"{self._max_payload} bytes. Chunk large messages or "
                f"increase max_payload."
            )

        ack = await self._js.publish(subject, payload, **kwargs)
        _logger.debug("Published %d bytes to '%s' (seq=%s)", len(payload), subject, ack.seq)
        return ack

    # ------------------------------------------------------------------
    # Subscribe (pull-based consumer with callback)
    # ------------------------------------------------------------------

    async def subscribe(
        self,
        subject: str,
        durable_name: str,
        callback: Callable[[Any], Any],
        **kwargs: Any,
    ) -> Any:
        """Create a durable pull consumer and invoke *callback* for every
        message received.  Messages are explicitly acknowledged after
        the callback returns; unhandled exceptions trigger a negative
        acknowledgment (NAK) so the message is redelivered.

        This method blocks until the task is cancelled or the
        connection is closed.  Run it as a background task for
        long-lived subscriptions::

            async def handle(msg):
                print(msg.data)

            # runs forever (until cancelled)
            await nq.subscribe("sensors.>", "pod-a", handle)

        Parameters
        ----------
        subject:
            NATS subject pattern to subscribe to.
        durable_name:
            Unique durable consumer name.  Messages acknowledged
            by this consumer are never redelivered to it, even
            across client restarts.
        callback:
            Async or sync callable invoked with each ``nats.aio.msg.Msg``.
            Return ``None`` to auto-ACK; raise to trigger NAK.
        **kwargs:
            Passed to ``JetStreamContext.pull_subscribe()``
            (e.g. ``stream``, ``flow_control``).
        Returns
        -------
            The ``JetStreamContext.PullSubscription`` object.
        """
        self._check_connected()

        psub = await self._js.pull_subscribe(
            subject,
            durable=durable_name,
            **kwargs,
        )
        _logger.info(
            "Subscribed to '%s' (durable=%s)", subject, durable_name
        )

        # Process messages until cancelled
        max_retries = 10
        retry_count = 0
        backoff = 1

        try:
            while True:
                try:
                    msgs = await psub.fetch(1, timeout=5)
                except TimeoutError:
                    # No messages available — keep waiting
                    continue
                except Exception:
                    retry_count += 1
                    if retry_count >= max_retries:
                        _logger.error(
                            "Subscribe to '%s' failed after %d retries — giving up",
                            subject, max_retries
                        )
                        raise
                    _logger.warning(
                        "Error fetching from '%s' (attempt %d/%d) — retrying in %ds",
                        subject, retry_count, max_retries, backoff, exc_info=True
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue

                for msg in msgs:
                    try:
                        maybe_coro = callback(msg)
                        if asyncio.iscoroutine(maybe_coro):
                            await maybe_coro
                        await msg.ack()
                    except Exception:
                        _logger.warning(
                            "Callback error on '%s' — NAK-ing message", subject, exc_info=True
                        )
                        try:
                            await msg.nak()
                        except Exception as nak_err:
                            _logger.error(
                                "NAK failed on '%s': %s", subject, nak_err, exc_info=True
                            )
                            raise  # Bubble up to outer retry/reconnect
        except asyncio.CancelledError:
            _logger.info("Subscribe cancelled for '%s'", subject)
            raise

        return psub

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _check_connected(self) -> None:
        """Raise if the NATS connection has not been established."""
        if self._nc is None or self._js is None:
            raise RuntimeError(
                "Not connected to NATS. Call `await nq.connect(...)` first."
            )


# ---------------------------------------------------------------------------
# Module-level factory (mirrors lma_core/config_utils.py pattern)
# ---------------------------------------------------------------------------

def create_queue(
    servers: str = "nats://localhost:4222",
    name: str = "lmao-queue",
) -> NatsQueue:
    """Create a ``NatsQueue`` instance (synchronous factory).

    The returned instance must still be connected via
    ``await nq.connect(servers)``.
    """
    return NatsQueue(name=name)
