"""
LMAO Server — Reticulum + LXMF message handler.

Runs on Raspberry Pi with an ESP32 RNode acting as a LoRa bridge.
Listens for LXMF messages from Cardputer clients and sends acknowledgements.
"""

import sys
import os
import logging
import time
import atexit
import shutil

import RNS
import LXMF

# Local imports
import config
from proto.lma_pb2 import LMAOEnvelope

from google.protobuf.message import DecodeError

logger = logging.getLogger(__name__)


def handle_lxmf_delivery(message):
    """
    Callback invoked when an LXMF message is received.

    Decodes incoming content as a protobuf LMAOEnvelope. The protocol uses
    title="p:Envelope" as a convention, but the handler attempts protobuf
    decode unconditionally and falls back to raw UTF-8 text for backward
    compatibility with non-protobuf senders. Sends a protobuf-encoded
    TextMessage ACK as a reply.
    """
    try:
        source_identity = message.get_source()
        source_hash = RNS.hexrep(source_identity.hash, delimit=False) if source_identity else "<unknown>"
        content_bytes = message.content if hasattr(message, 'content') else b""
        title = message.title_as_string() if hasattr(message, 'title_as_string') else ""

        print(f"\n--- Message Received ---")
        print(f"  From: {source_hash}")
        print(f"  Title: {title}")
        print(f"  Content length: {len(content_bytes)} bytes")

        # Try protobuf decode first (matching the documented protocol)
        display_text = None
        envelope = LMAOEnvelope()
        try:
            envelope.ParseFromString(content_bytes)
            if envelope.HasField('text'):
                text_msg = envelope.text
                display_text = text_msg.content
                print(f"  Content (protobuf): {display_text}")
        except DecodeError:
            logger.warning("Protobuf parse failed, falling back to raw text", exc_info=True)

        if display_text is None:
            # Fallback: treat content as raw UTF-8 text (backward compat)
            try:
                display_text = content_bytes.decode("utf-8")
                print(f"  Content (raw text): {display_text}")
            except UnicodeDecodeError:
                display_text = f"<non-text: {len(content_bytes)} bytes>"
                print(f"  Content: {display_text}")

        # Build and send a protobuf-encoded ACK reply
        reply_text = f"ACK from LMAO Server — received your message ({len(content_bytes)} bytes)"
        print(f"  Reply: {reply_text}")

        if source_identity is not None and router is not None:
            # Build protobuf envelope with TextMessage
            reply_envelope = LMAOEnvelope()
            reply_envelope.text.node_id = source_hash
            reply_envelope.text.content = reply_text
            reply_envelope.text.timestamp = int(time.time() * 1000)

            reply_msg = LXMF.LXMessage(
                destination=source_identity,
                source=server_identity,
                content=reply_envelope.SerializeToString(),
                title="p:Envelope",
                desired_method=LXMF.LXMessage.OPPORTUNISTIC,
            )
            router.handle_outbound(reply_msg)
            print(f"  Reply sent.")
        else:
            print(f"  WARNING: Could not send reply (no source identity or router).")

    except AttributeError as e:
        logger.error("LXMF message missing expected attributes: %s", e, exc_info=True)
    except (RNS.RNSException, LXMF.LXMFException) as e:
        logger.error("RNS/LXMF error processing message: %s", e, exc_info=True)
    except Exception as e:
        logger.error("Unexpected error in handle_lxmf_delivery: %s", e, exc_info=True)


# Globals set in main()
router = None
server_identity = None


def main():
    """Initialize Reticulum, LXMF router, and enter main loop."""
    global router, server_identity

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # Check if the RNode port exists before initializing
    cfg_dict = config.get_config_dict()
    rnode_port = cfg_dict['interfaces']['RNode LoRa']['port']
    if not os.path.exists(rnode_port):
        print(f"⚠️  RNode port {rnode_port} not found.")
        print(f"   The server will start with WiFi AutoInterface only.")
        print(f"   Set the LMAO_RNODE_PORT environment variable if your RNode is on a different port.")
        print(f"   Example: LMAO_RNODE_PORT=/dev/ttyACM0 python3 server.py")
        print(f"   LoRa messaging will be unavailable until an RNode is connected.\n")

    # Initialize Reticulum with our config
    print("Initializing Reticulum...")
    try:
        configdir = config.get_configdir()
        atexit.register(lambda: shutil.rmtree(configdir, ignore_errors=True))
        RNS.Reticulum(configdir=configdir)  # Initialize singleton (return value unused)
    except (OSError, PermissionError) as e:
        print(f"FATAL: Failed to create config directory for Reticulum: {e}", file=sys.stderr)
        print("Check that /tmp is writable and disk is not full.", file=sys.stderr)
        sys.exit(1)
    except RNS.RNSException as e:
        print(f"FATAL: Reticulum initialization failed: {e}", file=sys.stderr)
        print(f"This is often caused by a missing or misconfigured RNode on {rnode_port}.")
        print("Check that:")
        print(f"  1. The RNode is plugged in and on the correct port ({rnode_port})")
        print(f"  2. You have permission: sudo usermod -a -G dialout $USER")
        print(f"  3. The RNode firmware is flashed correctly")
        print("  See rnode_firmware/README.md and README Troubleshooting.")
        sys.exit(1)
    except Exception as e:
        print(f"FATAL: Failed to initialize Reticulum: {e}", file=sys.stderr)
        print("Check your config and RNode connection. See README Troubleshooting.", file=sys.stderr)
        sys.exit(1)
    print("Reticulum initialized.")

    # Create identity for the server
    try:
        server_identity = RNS.Identity()
    except Exception as e:
        print(f"FATAL: Failed to create server identity: {e}", file=sys.stderr)
        sys.exit(1)
    identity_hex = RNS.hexrep(server_identity.hash, delimit=False)

    # Create LXMF router with our identity
    print("Starting LXMF router...")
    try:
        router = LXMF.LXMRouter(identity=server_identity, storagepath="/tmp/lmao_server_lxmf")
        router.register_delivery_callback(handle_lxmf_delivery)
    except Exception as e:
        print(f"FATAL: Failed to start LXMF router: {e}", file=sys.stderr)
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


if __name__ == "__main__":
    main()
