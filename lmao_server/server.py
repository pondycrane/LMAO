"""
LMAO Server — Reticulum + LXMF message handler.

Runs on Raspberry Pi with an ESP32 RNode acting as a LoRa bridge.
Listens for LXMF text messages from Cardputer clients and sends acknowledgements.
"""

import sys
import time

import RNS
import LXMF

# Local imports
import config


def handle_lxmf_delivery(message):
    """
    Callback invoked when an LXMF message is received.

    The message content is a protobuf-encoded LMAOEnvelope with Title="p:Envelope".
    For this POC, we expect TextMessage payloads and echo them back as a reply.
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
        print(f"  Content preview: {content_bytes[:200]}")

        # For POC: if this looks like a text message, send a simple ACK reply
        reply_text = f"ACK from LMAO Server — received your message ({len(content_bytes)} bytes)"
        print(f"  Reply: {reply_text}")

        # Send reply as LXMF message back to the source
        if source_identity is not None and router is not None:
            reply_msg = LXMF.LXMessage(
                destination=source_identity,
                source=server_identity,
                content=reply_text.encode("utf-8"),
                title="p:Envelope",
                desired_method=LXMF.LXMessage.OPPORTUNISTIC,
            )
            router.handle_outbound(reply_msg)
            print(f"  Reply sent.")
        else:
            print(f"  WARNING: Could not send reply (no source identity or router).")

    except Exception as e:
        print(f"ERROR in handle_lxmf_delivery: {e}", file=sys.stderr)


# Globals set in main()
router = None
server_identity = None


def main():
    """Initialize Reticulum, LXMF router, and enter main loop."""
    global router, server_identity

    # Initialize Reticulum with our config
    print("Initializing Reticulum...")
    configdir = config.get_configdir()
    reticulum = RNS.Reticulum(configdir=configdir)
    print("Reticulum initialized.")

    # Create identity for the server
    server_identity = RNS.Identity()
    identity_hex = RNS.hexrep(server_identity.hash, delimit=False)

    # Create LXMF router with our identity
    print("Starting LXMF router...")
    router = LXMF.LXMRouter(identity=server_identity, storagepath="/tmp/lmao_server_lxmf")
    router.register_delivery_callback(handle_lxmf_delivery)

    # Announce ourselves
    print(f"\n{'='*50}")
    print(f"LMAO Server POC — Running")
    print(f"Node identity: {identity_hex}")
    print(f"Listening for LXMF messages...")
    print(f"  LoRa: RNode on {config.get_config_dict()['interfaces']['RNode LoRa']['port']}")
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
