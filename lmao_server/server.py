"""
LMAO Server — Reticulum + LXMF message handler with optional gRPC API
and NATS JetStream publishing.

Runs on Raspberry Pi with an ESP32 RNode acting as a LoRa bridge.
Listens for LXMF messages from Cardputer clients, sends acknowledgements,
and publishes incoming message payloads to NATS JetStream (subject
"lmao.messages.env") for downstream consumption by K8s pods.

When gRPC is enabled (default), also serves the LMAO gRPC API on port 50051
for K8s pod integration. The gRPC service provides:
  - Send:     Inject LMAOEnvelope into the LXMF mesh
  - Subscribe: Stream incoming LXMF messages to gRPC clients
  - GetIdentity: Return the server's Reticulum identity hex

All optional dependencies (gRPC, NATS) use lazy imports with graceful
degradation — the server starts and operates without them.
"""

import asyncio
import logging
import os
import time

# Local imports
from lmao_server import config
from google.protobuf.message import DecodeError

from lma_core import LMAOEnvelope
from lma_core.message_utils import decode_lmao_message
from lma_core.rns_di import LXMF, RNS
from lma_core.rns_init import init_rns_and_lxmf as _shared_init
from lma_core.rns_init import warn_if_rnode_missing

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# gRPC imports (optional — gracefully degrade if unavailable)
# ──────────────────────────────────────────────────────────────
try:
    import grpc

    from lma_core.grpc_types import (
        GetIdentityResponse,
        LMAOServicer,
        SendResponse,
        SubscribeResponse,
    )

    GRPC_AVAILABLE = True
except ImportError:
    GRPC_AVAILABLE = False
    logger.info("gRPC not available — K8s integration features disabled.")

# ──────────────────────────────────────────────────────────────
# NATS imports (optional — gracefully degrade if unavailable)
# ──────────────────────────────────────────────────────────────
try:
    from lma_core.queue import NatsQueue, _NATS_AVAILABLE as _NATS_PY_AVAILABLE

    NATS_AVAILABLE = _NATS_PY_AVAILABLE
except ImportError:
    NATS_AVAILABLE = False
    logger.info("nats-py not available — NATS JetStream publishing disabled.")

# Default NATS server address — overridable via environment variable.
# When running inside Docker with --network host, "localhost" resolves
# to the host, so the K8s NATS service must be reachable via NodePort
# or the host's cluster network.
_NATS_SERVER = os.environ.get("NATS_SERVER", "nats://localhost:4222")


def _warn_if_rnode_missing(rnode_port):
    """Warn if the RNode port does not exist (delegates to shared helper)."""
    warn_if_rnode_missing(rnode_port, role="server")


def _init_rns_and_lxmf(rnode_port, identity_storage_path="/tmp/lmao_server_lxmf"):
    """Initialize Reticulum + LXMF for the server (delegates to shared helper)."""
    return _shared_init(
        rnode_port=rnode_port,
        configdir_factory=config.get_configdir,
        identity_storage_path=identity_storage_path,
    )


def _print_startup_banner(identity_hex, rnode_port, grpc_available, nats_connected=False):
    """Print the server startup banner with identity and status info."""
    rnode_status = (
        f"RNode on {rnode_port}"
        if os.path.exists(rnode_port)
        else "⚠️  RNode not connected — LoRa unavailable"
    )
    nats_status = (
        f"NATS: {_NATS_SERVER}"
        if nats_connected
        else "NATS: disconnected"
    )
    print(f"\n{'=' * 50}")
    print("LMAO Server — Running (async mode)")
    print(f"Node identity: {identity_hex}")
    print("Listening for LXMF messages...")
    print(f"  LoRa: {rnode_status}")
    print("  WiFi: AutoInterface enabled")
    print("  Title discriminator: p:Envelope")
    print(f"  {nats_status}")
    if grpc_available:
        print("  gRPC: 0.0.0.0:50051")
    print(f"{'=' * 50}\n")


_NATS_SUBJECT = "lmao.messages.env"


class Server:
    """Encapsulates LMAO server lifecycle: Reticulum init, LXMF router, and message handling."""

    def __init__(self, config_dict=None):
        self.router = None
        self.server_identity = None
        self._config_dict = config_dict
        # gRPC subscriber queues (set by LMAOGrpcService if active)
        self._grpc_subscribers = []
        # NATS JetStream publisher (injected by async_main)
        self._nats_queue = None
        self._loop = None

    def register_grpc_subscriber(self, queue):
        """Register an asyncio.Queue for gRPC Subscribe streaming."""
        self._grpc_subscribers.append(queue)

    def unregister_grpc_subscriber(self, queue):
        """Remove a previously registered subscriber queue."""
        if queue in self._grpc_subscribers:
            self._grpc_subscribers.remove(queue)

    def clear_grpc_subscribers(self):
        """Drain and clear all subscriber queues on shutdown."""
        for q in list(self._grpc_subscribers):
            try:
                q.put_nowait(None)  # Sentinel to unblock subscribers
            except Exception:
                pass
        self._grpc_subscribers.clear()

    def _fanout_to_grpc_subscribers(self, message):
        """Push an incoming LXMF message to all gRPC subscriber queues."""
        dead = []
        for queue in self._grpc_subscribers:
            try:
                queue.put_nowait(message)
            except Exception:
                logger.warning("gRPC subscriber error — dropping subscriber", exc_info=True)
                dead.append(queue)
        for q in dead:
            self.unregister_grpc_subscriber(q)

    def handle_lxmf_delivery(self, message):
        """Decodes incoming content as a protobuf LMAOEnvelope. The protocol uses
        title="p:Envelope" as a convention, but the handler attempts protobuf
        decode unconditionally and falls back to raw UTF-8 text for backward
        compatibility with non-protobuf senders. Sends a protobuf-encoded
        TextMessage ACK as a reply.

        Also publishes the incoming message payload to NATS JetStream
        (fire-and-forget via asyncio.run_coroutine_threadsafe) and fans out
        to gRPC subscribers so streaming clients receive the message.
        """
        try:
            source_identity = message.get_source()
            source_hash = (
                RNS.hexrep(source_identity.hash, delimit=False) if source_identity else "<unknown>"
            )
            content_bytes = message.content if hasattr(message, "content") else b""
            title = message.title_as_string() if hasattr(message, "title_as_string") else ""

            logger.info(
                "Message received — From: %s  Title: %s  Content length: %d bytes",
                source_hash,
                title,
                len(content_bytes),
            )

            # Decode content (protobuf first, UTF-8 fallback, byte-count placeholder)
            decode_lmao_message(content_bytes)

            # Build and send a protobuf-encoded ACK reply
            reply_text = (
                f"ACK from LMAO Server — received your message ({len(content_bytes)} bytes)"
            )
            logger.info("Reply: %s", reply_text)

            if source_identity is not None and self.router is not None:
                # Build protobuf envelope with TextMessage
                reply_envelope = LMAOEnvelope()
                reply_envelope.text.node_id = source_hash
                reply_envelope.text.content = reply_text
                reply_envelope.text.timestamp = int(time.time() * 1000)

                reply_msg = LXMF.LXMessage(
                    destination=source_identity,
                    source=self.server_identity,
                    content=reply_envelope.SerializeToString(),
                    title="p:Envelope",
                    desired_method=LXMF.LXMessage.OPPORTUNISTIC,
                )
                self.router.handle_outbound(reply_msg)
                logger.info("Reply sent.")
            else:
                logger.warning("Could not send reply (no source identity or router).")

            # Publish to NATS JetStream (fire-and-forget from sync context)
            if self._nats_queue is not None and self._loop is not None:
                fut = asyncio.run_coroutine_threadsafe(
                    self._publish_to_nats(source_hash, content_bytes),
                    self._loop,
                )
                fut.add_done_callback(
                    lambda f: logger.error("NATS publish task failed: %s", f.exception())
                    if f.exception() else None
                )
            else:
                logger.debug("NATS unavailable — skipping publish")

            # Fan out to gRPC subscribers (if any)
            self._fanout_to_grpc_subscribers(message)

        except AttributeError as e:
            logger.error("LXMF message missing expected attributes: %s", e, exc_info=True)
        except (RNS.RNSException, LXMF.LXMFException) as e:
            logger.error("RNS/LXMF error processing message: %s", e, exc_info=True)
        except Exception as e:
            logger.error("Unexpected error in handle_lxmf_delivery: %s", e, exc_info=True)

    async def _publish_to_nats(self, source_hash: str, content_bytes: bytes) -> None:
        """Publish an incoming LXMF message payload to NATS JetStream.

        Called fire-and-forget from the sync ``handle_lxmf_delivery``
        callback via ``asyncio.run_coroutine_threadsafe``.

        Args:
            source_hash: Hex identity of the sending node.
            content_bytes: Raw content bytes from the LXMF message.
        """
        if self._nats_queue is None:
            return
        try:
            ack = await self._nats_queue.publish(_NATS_SUBJECT, content_bytes)
            logger.debug(
                "Published %d bytes from %s to NATS (seq=%s)",
                len(content_bytes),
                source_hash,
                ack.seq,
            )
        except asyncio.CancelledError:
            logger.debug("NATS publish cancelled during shutdown — skipping")
            raise
        except Exception:
            logger.warning("NATS publish failed", exc_info=True)


# ──────────────────────────────────────────────────────────────
# gRPC Service Implementation
# ──────────────────────────────────────────────────────────────

if GRPC_AVAILABLE:

    class LMAOGrpcService(LMAOServicer):
        """Implements the LMAO gRPC service, bridging into the LXMF mesh."""

        def __init__(self, server_instance: Server):
            self._server = server_instance
            self._router = server_instance.router

        async def Send(self, request, context):
            """Handle a Send RPC: deserialize envelope and dispatch into LXMF."""
            envelope = LMAOEnvelope()
            try:
                envelope.ParseFromString(request.envelope)
            except (DecodeError, ValueError) as e:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"Bad envelope: {e}")

            # Resolve destination identity from the request hash
            dest_hash = request.destination_hash
            try:
                dest = RNS.Identity.from_hex(dest_hash) if dest_hash else None
            except (ValueError, RNS.RNSException, TypeError):
                dest = None
            if not dest:
                return SendResponse(
                    destination_hash=dest_hash,
                    status="error: invalid or unreachable destination",
                )

            # Build an LXMF message and dispatch via the router
            try:
                lxmf_msg = LXMF.LXMessage(
                    destination=dest,
                    source=self._server.server_identity,
                    content=envelope.SerializeToString(),
                    title="p:Envelope",
                    desired_method=LXMF.LXMessage.OPPORTUNISTIC,
                )
                self._router.handle_outbound(lxmf_msg)
                return SendResponse(
                    destination_hash=dest_hash,
                    status="queued",
                )
            except (RNS.RNSException, LXMF.LXMFException, OSError) as e:
                logger.error("Send RPC failed: %s", e, exc_info=True)
                await context.abort(grpc.StatusCode.INTERNAL, f"Send failed: {e}")

        async def Subscribe(self, request, context):
            """Stream incoming LXMF messages to the client.

            If request.title_filter is set, only messages matching
            that title are forwarded to the client.
            """
            queue = asyncio.Queue(maxsize=128)
            self._server.register_grpc_subscriber(queue)
            try:
                while True:
                    message = await queue.get()
                    if message is None:  # Sentinel received during shutdown
                        break
                    try:
                        # Apply optional title filter
                        title = getattr(message, "title_as_string", lambda: "")()
                        if request.title_filter and request.title_filter not in title:
                            continue
                        # Build response
                        content_bytes = getattr(message, "content", b"")
                        source_identity = message.get_source()
                        source_hash = (
                            RNS.hexrep(source_identity.hash, delimit=False)
                            if source_identity
                            else ""
                        )
                        resp = SubscribeResponse(
                            envelope=content_bytes,
                            source_hash=source_hash,
                        )
                        yield resp
                    except (AttributeError, RNS.RNSException, LXMF.LXMFException) as e:
                        logger.warning("Subscribe: skipping malformed message: %s", e)
                        continue
            except asyncio.CancelledError:
                pass
            finally:
                self._server.unregister_grpc_subscriber(queue)

        async def GetIdentity(self, request, context):
            """Return the server's Reticulum identity hex."""
            identity_hex = RNS.hexrep(self._server.server_identity.hash, delimit=False)
            return GetIdentityResponse(
                identity_hex=identity_hex,
                node_name="lmao-server",
            )

else:

    class LMAOGrpcService:  # type: ignore[no-redef]
        """Placeholder when gRPC is not available — all methods raise ImportError."""

        def __init__(self, server_instance):
            raise ImportError("gRPC is not installed. Install grpcio and grpcio-tools.")


# ──────────────────────────────────────────────────────────────
# Async Entry Point (with gRPC)
# ──────────────────────────────────────────────────────────────


async def async_main():
    """Async entry point: initialize LXMF router and optionally start gRPC server.

    This is the recommended way to run the server when K8s/gRPC integration
    is desired. Falls back gracefully if gRPC is not available.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    cfg_dict = config.get_config_dict()
    rnode_port = cfg_dict["interfaces"]["RNode LoRa"]["port"]
    _warn_if_rnode_missing(rnode_port)

    # Use shared initialization helper (handles specific exception types)
    server_identity, router = _init_rns_and_lxmf(rnode_port)

    # Create Server instance (wraps router + identity)
    lmao_server = Server(config_dict=cfg_dict)
    lmao_server.server_identity = server_identity
    lmao_server.router = router

    # Register the delivery callback
    router.register_delivery_callback(lmao_server.handle_lxmf_delivery)

    # ── NATS connect (optional) ─────────────────────────────────
    nats_queue = None
    if NATS_AVAILABLE:
        try:
            nats_queue = NatsQueue(name="lmao-server")
            await nats_queue.connect(servers=_NATS_SERVER)
            await nats_queue.ensure_stream("LMAO_MESSAGES", ["lmao.messages.>"])
            logger.info("NATS JetStream connected: %s", _NATS_SERVER)

            # Inject into the server instance for publishing from callbacks
            lmao_server._nats_queue = nats_queue
            lmao_server._loop = asyncio.get_event_loop()
        except Exception as exc:
            logger.warning(
                "NATS connection failed (%s) — continuing without NATS publishing.",
                exc,
                exc_info=True,
            )
            nats_queue = None

    # Print banner
    _print_startup_banner(
        RNS.hexrep(server_identity.hash, delimit=False),
        rnode_port,
        GRPC_AVAILABLE,
        nats_queue is not None,
    )

    # Start gRPC server if available
    grpc_server = None
    if GRPC_AVAILABLE:
        grpc_service = LMAOGrpcService(lmao_server)
        grpc_server = grpc.aio.server()
        from lma_core.grpc_types import add_LMAOServicer_to_server

        add_LMAOServicer_to_server(grpc_service, grpc_server)
        grpc_server.add_insecure_port("0.0.0.0:50051")
        await grpc_server.start()
        logger.info("gRPC server started on 0.0.0.0:50051")
        print("gRPC server ready on 0.0.0.0:50051")

    # Keep running
    try:
        if grpc_server:
            await grpc_server.wait_for_termination()
        else:
            # No gRPC — just sleep until interrupted
            while True:
                await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        if grpc_server:
            await grpc_server.stop(5)
        if lmao_server:
            lmao_server.clear_grpc_subscribers()
            lmao_server._nats_queue = None  # Prevent new NATS publish attempts during shutdown
        if nats_queue is not None:
            await nats_queue.close()
            logger.info("NATS connection closed.")


if __name__ == "__main__":
    # When run directly, prefer the gRPC-enabled async main
    asyncio.run(async_main())
