"""
LMAO Server — Reticulum + LXMF message handler with optional gRPC API.

Runs on Raspberry Pi with an ESP32 RNode acting as a LoRa bridge.
Listens for LXMF messages from Cardputer clients and sends acknowledgements.

When gRPC is enabled (default), also serves the LMAO gRPC API on port 50051
for K8s pod integration. The gRPC service provides:
  - Send:     Inject LMAOEnvelope into the LXMF mesh
  - Subscribe: Stream incoming LXMF messages to gRPC clients
  - Tunnel:   Bidirectional raw LXMF packet tunnel
  - GetIdentity: Return the server's Reticulum identity hex
"""

import sys
import os
import logging
import time
import asyncio
import atexit
import shutil

import RNS
import LXMF

# Local imports
import config
from lma_core import LMAOEnvelope

from google.protobuf.message import DecodeError

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# gRPC imports (optional — gracefully degrade if unavailable)
# ──────────────────────────────────────────────────────────────
try:
    import grpc
    from lma_core import (
        SendRequest, SendResponse,
        SubscribeRequest, SubscribeResponse,
        TunnelRequest, TunnelResponse,
        GetIdentityRequest, GetIdentityResponse,
        LMAOServicer,
    )
    GRPC_AVAILABLE = True
except ImportError:
    GRPC_AVAILABLE = False
    logger.info("gRPC not available — K8s integration features disabled.")


class Server:
    """Encapsulates LMAO server lifecycle: Reticulum init, LXMF router, and message handling."""

    def __init__(self, config_dict=None):
        self.router = None
        self.server_identity = None
        self._config_dict = config_dict
        # gRPC subscriber queues (set by LMAOGrpcService if active)
        self._grpc_subscribers = []

    def register_grpc_subscriber(self, queue):
        """Register an asyncio.Queue for gRPC Subscribe streaming."""
        self._grpc_subscribers.append(queue)

    def unregister_grpc_subscriber(self, queue):
        """Remove a previously registered subscriber queue."""
        if queue in self._grpc_subscribers:
            self._grpc_subscribers.remove(queue)

    def _fanout_to_grpc_subscribers(self, message):
        """Push an incoming LXMF message to all gRPC subscriber queues."""
        dead = []
        for queue in self._grpc_subscribers:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                dead.append(queue)
            except Exception:
                dead.append(queue)
        for q in dead:
            self.unregister_grpc_subscriber(q)

    def handle_lxmf_delivery(self, message):
        """Decodes incoming content as a protobuf LMAOEnvelope. The protocol uses
        title="p:Envelope" as a convention, but the handler attempts protobuf
        decode unconditionally and falls back to raw UTF-8 text for backward
        compatibility with non-protobuf senders. Sends a protobuf-encoded
        TextMessage ACK as a reply.

        Also fans out to gRPC subscribers so streaming clients receive the message.
        """
        try:
            source_identity = message.get_source()
            source_hash = RNS.hexrep(source_identity.hash, delimit=False) if source_identity else "<unknown>"
            content_bytes = message.content if hasattr(message, 'content') else b""
            title = message.title_as_string() if hasattr(message, 'title_as_string') else ""

            logger.info("Message received — From: %s  Title: %s  Content length: %d bytes",
                         source_hash, title, len(content_bytes))

            # Try protobuf decode first (matching the documented protocol)
            display_text = None
            envelope = LMAOEnvelope()
            try:
                envelope.ParseFromString(content_bytes)
                if envelope.HasField('text'):
                    text_msg = envelope.text
                    display_text = text_msg.content
                    logger.info("Content (protobuf): %s", display_text)
                else:
                    # Envelope decoded but contains a non-text payload.
                    # Only text messages are supported in this POC.
                    logger.warning(
                        "Envelope contains non-text payload. "
                        "Only text messages are supported in this POC. Falling back."
                    )
            except DecodeError:
                logger.warning("Protobuf parse failed, falling back to raw text")

            if display_text is None:
                # Fallback: treat content as raw UTF-8 text (backward compat)
                try:
                    display_text = content_bytes.decode("utf-8")
                    logger.info("Content (raw text): %s", display_text)
                except UnicodeDecodeError:
                    display_text = f"<non-text: {len(content_bytes)} bytes>"
                    logger.info("Content: %s", display_text)

            # Build and send a protobuf-encoded ACK reply
            reply_text = f"ACK from LMAO Server — received your message ({len(content_bytes)} bytes)"
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

            # Fan out to gRPC subscribers (if any)
            self._fanout_to_grpc_subscribers(message)

        except AttributeError as e:
            logger.error("LXMF message missing expected attributes: %s", e, exc_info=True)
        except (RNS.RNSException, LXMF.LXMFException) as e:
            logger.error("RNS/LXMF error processing message: %s", e, exc_info=True)
        except Exception as e:
            logger.error("Unexpected error in handle_lxmf_delivery: %s", e, exc_info=True)

    def start(self):
        """Initialize Reticulum, LXMF router, and enter main loop (sync version).

        This is the legacy synchronous entry point. For the async version
        (with gRPC), see async_main() below.
        """
        # Configure logging
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
        )

        cfg_dict = self._config_dict if self._config_dict is not None else config.get_config_dict()
        # Check if the RNode port exists before initializing
        rnode_port = cfg_dict['interfaces']['RNode LoRa']['port']
        if not os.path.exists(rnode_port):
            logger.warning("RNode port %s not found. LoRa messaging will be unavailable.", rnode_port)
            print(
                f"⚠️  RNode port {rnode_port} not found.\n"
                f"   The server will start with WiFi AutoInterface only.\n"
                f"   Set the LMAO_RNODE_PORT environment variable if your RNode is on a different port.\n"
                f"   Example: LMAO_RNODE_PORT=/dev/ttyACM0 python3 server.py\n"
                f"   LoRa messaging will be unavailable until an RNode is connected.\n"
            )

        # Initialize Reticulum with our config
        print("Initializing Reticulum...")
        try:
            configdir = config.get_configdir()
            atexit.register(lambda: shutil.rmtree(configdir, ignore_errors=True))
            RNS.Reticulum(configdir=configdir)  # Initialize singleton (return value unused)
        except (OSError, PermissionError) as e:
            logger.critical("Failed to create config directory for Reticulum: %s", e, exc_info=True)
            print(f"FATAL: Failed to create config directory for Reticulum: {e}", file=sys.stderr)
            print("Check that /tmp is writable and disk is not full.", file=sys.stderr)
            sys.exit(1)
        except RNS.RNSException as e:
            logger.critical("Reticulum initialization failed: %s", e, exc_info=True)
            print(f"FATAL: Reticulum initialization failed: {e}", file=sys.stderr)
            print(f"This is often caused by a missing or misconfigured RNode on {rnode_port}.")
            print("Check that:")
            print(f"  1. The RNode is plugged in and on the correct port ({rnode_port})")
            print(f"  2. You have permission: sudo usermod -a -G dialout $USER")
            print(f"  3. The RNode firmware is flashed correctly")
            print("  See rnode_firmware/README.md and README Troubleshooting.")
            sys.exit(1)
        except Exception as e:
            logger.critical("Failed to initialize Reticulum: %s", e, exc_info=True)
            print(f"FATAL: Failed to initialize Reticulum: {e}", file=sys.stderr)
            print("Check your config and RNode connection. See README Troubleshooting.", file=sys.stderr)
            sys.exit(1)
        print("Reticulum initialized.")

        # Create identity for the server
        try:
            self.server_identity = RNS.Identity()
        except Exception as e:
            logger.critical("Failed to create server identity: %s", e, exc_info=True)
            print("FATAL: Failed to create server identity. See log for details.", file=sys.stderr)
            sys.exit(1)
        identity_hex = RNS.hexrep(self.server_identity.hash, delimit=False)

        # Create LXMF router with our identity
        print("Starting LXMF router...")
        try:
            self.router = LXMF.LXMRouter(identity=self.server_identity, storagepath="/tmp/lmao_server_lxmf")
            self.router.register_delivery_callback(self.handle_lxmf_delivery)
        except Exception as e:
            logger.critical("Failed to start LXMF router: %s", e, exc_info=True)
            print("FATAL: Failed to start LXMF router. See log for details.", file=sys.stderr)
            sys.exit(1)

        # Print startup banner
        rnode_status = f"RNode on {rnode_port}" if os.path.exists(rnode_port) else "⚠️  RNode not connected — LoRa unavailable"
        print(f"\n{'='*50}")
        print(f"LMAO Server POC — Running")
        print(f"Node identity: {identity_hex}")
        print(f"Listening for LXMF messages...")
        print(f"  LoRa: {rnode_status}")
        print(f"  WiFi: AutoInterface enabled")
        print(f"  Title discriminator: p:Envelope")
        print(f"{'='*50}\n")

        # Main event loop
        try:
            while True:
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\nShutting down...")
            sys.exit(0)


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
            from lma_core import LMAOEnvelope as Envelope
            envelope = Envelope()
            try:
                envelope.ParseFromString(request.envelope)
            except Exception as e:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"Bad envelope: {e}")
                return SendResponse(status=f"error: {e}")

            # Build an LXMF message and dispatch via the router
            try:
                dest_hash = request.destination_hash if hasattr(request, 'destination_hash') else ""
                # For now, send via the router's default identity path
                # In a full implementation, destination_hash would be resolved
                # to an RNS.Identity.
                lxmf_msg = LXMF.LXMessage(
                    destination=RNS.Identity(),  # Will be resolved properly
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
            except Exception as e:
                logger.error("Send RPC failed: %s", e, exc_info=True)
                return SendResponse(status=f"error: {e}")

        async def Subscribe(self, request, context):
            """Stream incoming LXMF messages to the client."""
            queue = asyncio.Queue(maxsize=128)
            self._server.register_grpc_subscriber(queue)
            try:
                while True:
                    message = await queue.get()
                    # Build response
                    content_bytes = message.content if hasattr(message, 'content') else b""
                    source_identity = message.get_source()
                    source_hash = RNS.hexrep(source_identity.hash, delimit=False) if source_identity else ""
                    resp = SubscribeResponse(
                        envelope=content_bytes,
                        source_hash=source_hash,
                    )
                    yield resp
            except asyncio.CancelledError:
                pass
            finally:
                self._server.unregister_grpc_subscriber(queue)

        async def Tunnel(self, request_iterator, context):
            """Bidirectional tunnel — forward incoming packets to LXMF and yield responses."""
            async for request in request_iterator:
                try:
                    # Deserialize the raw packet
                    packet = LXMF.LXMessage()
                    # In a full implementation, we'd reconstruct an LXMF message
                    # from the raw packet bytes. For now, we echo back.
                    yield TunnelResponse(
                        packet=request.packet,
                        source_hash="",
                        status="echoed",
                    )
                except Exception as e:
                    yield TunnelResponse(
                        packet=b"",
                        source_hash="",
                        status=f"error: {e}",
                    )

        async def GetIdentity(self, request, context):
            """Return the server's Reticulum identity hex."""
            identity_hex = RNS.hexrep(self._server.server_identity.hash, delimit=False)
            return GetIdentityResponse(
                identity_hex=identity_hex,
                node_name="lmao-server",
            )

else:

    class LMAOGrpcService:
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
    rnode_port = cfg_dict['interfaces']['RNode LoRa']['port']

    if not os.path.exists(rnode_port):
        logger.warning("RNode port %s not found. LoRa messaging will be unavailable.", rnode_port)
        print(
            f"⚠️  RNode port {rnode_port} not found.\n"
            f"   The server will start with WiFi AutoInterface only.\n"
            f"   Set the LMAO_RNODE_PORT environment variable if your RNode is on a different port.\n"
            f"   LoRa messaging will be unavailable until an RNode is connected.\n"
        )

    # Initialize Reticulum
    print("Initializing Reticulum...")
    try:
        configdir = config.get_configdir()
        atexit.register(lambda: shutil.rmtree(configdir, ignore_errors=True))
        RNS.Reticulum(configdir=configdir)
    except Exception as e:
        logger.critical("Reticulum init failed: %s", e, exc_info=True)
        print(f"FATAL: Reticulum init failed: {e}", file=sys.stderr)
        sys.exit(1)
    print("Reticulum initialized.")

    # Create server identity
    try:
        server_identity = RNS.Identity()
    except Exception as e:
        logger.critical("Failed to create identity: %s", e, exc_info=True)
        sys.exit(1)
    identity_hex = RNS.hexrep(server_identity.hash, delimit=False)

    # Create LXMF router
    print("Starting LXMF router...")
    try:
        router = LXMF.LXMRouter(identity=server_identity, storagepath="/tmp/lmao_server_lxmf")
    except Exception as e:
        logger.critical("Failed to start LXMF router: %s", e, exc_info=True)
        sys.exit(1)

    # Create Server instance (wraps router + identity)
    lmao_server = Server(config_dict=cfg_dict)
    lmao_server.server_identity = server_identity
    lmao_server.router = router

    # Register the delivery callback
    router.register_delivery_callback(lmao_server.handle_lxmf_delivery)

    # Print banner
    rnode_status = f"RNode on {rnode_port}" if os.path.exists(rnode_port) else "⚠️  RNode not connected — LoRa unavailable"
    print(f"\n{'='*50}")
    print(f"LMAO Server — Running (async mode)")
    print(f"Node identity: {identity_hex}")
    print(f"Listening for LXMF messages...")
    print(f"  LoRa: {rnode_status}")
    print(f"  WiFi: AutoInterface enabled")
    print(f"  Title discriminator: p:Envelope")
    if GRPC_AVAILABLE:
        print(f"  gRPC: 0.0.0.0:50051")
    print(f"{'='*50}\n")

    # Start gRPC server if available
    grpc_server = None
    if GRPC_AVAILABLE:
        grpc_service = LMAOGrpcService(lmao_server)
        grpc_server = grpc.aio.server()
        lma_pb2_grpc = sys.modules.get('proto.lma_pb2_grpc')
        if lma_pb2_grpc:
            lma_pb2_grpc.LMAO.add_LMAOServicer_to_server(grpc_service, grpc_server)
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


def main():
    """Thin entry point: creates a Server instance and starts it (sync mode).

    For gRPC-enabled mode, run async_main() instead:
        import asyncio
        from lmao_server.server import async_main
        asyncio.run(async_main())
    """
    Server(config.get_config_dict()).start()


if __name__ == "__main__":
    # When run directly, prefer the gRPC-enabled async main
    asyncio.run(async_main())
