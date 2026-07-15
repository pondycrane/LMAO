"""
LMAO Human Client — Interactive CLI for the LMAO network.

Runs on a laptop or desktop as a first-party terminal client for human
operators.  Initialises Reticulum with WiFi AutoInterface (always enabled)
plus optional RNode for LoRa, creates an LXMF identity, announces presence,
and enters a REPL loop for composing and reading text messages.

Messages use the project's protobuf protocol: LMAOEnvelope → TextMessage
with title="p:Envelope", matching the server handler exactly.

Usage:
    bazel run //human_client:client
    python3 human_client/client.py
"""

import logging
import os
import time

# Local imports
import config

from lma_core import LMAOEnvelope
from lma_core.message_utils import decode_lmao_message
from lma_core.rns_di import LXMF, RNS
from lma_core.rns_init import init_rns_and_lxmf, warn_if_rnode_missing

logger = logging.getLogger(__name__)


def _identity_to_destination(identity):
    """Wrap an RNS.Identity in an RNS.Destination for LXMF.

    LXMF requires ``RNS.Destination`` objects for both ``destination`` and
    ``source`` parameters of ``LXMessage.__init__()``. The destination hash
    is deterministic from the identity hash, app name, and aspect, so the
    resulting address is stable and matchable.
    """
    return RNS.Destination(
        identity,
        RNS.Destination.OUT,
        RNS.Destination.SINGLE,
        "lxmf",
        "delivery",
    )


class Client:
    """Encapsulates human client lifecycle: Reticulum init, LXMF router,
    message handling (receive and send), and interactive REPL loop."""

    def __init__(self, config_dict=None):
        self.router = None
        self.client_identity = None
        self._config_dict = config_dict
        self._default_dest_hash = None
        self._default_dest_identity = None

    def handle_lxmf_delivery(self, message):
        """Decodes incoming content as a protobuf LMAOEnvelope. On success,
        extracts TextMessage content and displays it to the user. If the
        envelope decodes but does not contain a TextMessage (e.g., a
        SensorReport or Command), falls back to raw text. Falls back to
        raw UTF-8 text for backward compatibility with non-protobuf
        senders. Does NOT send an ACK reply (unlike the server) — the
        human operator decides whether to respond.

        Args:
            message: An LXMF message object with get_source(), content,
                and title_as_string() attributes.
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
            display_text = decode_lmao_message(content_bytes)

            # Print incoming message to terminal, restoring the prompt afterward
            print(f"\n>>> MSG from {source_hash}: {display_text}")

        except AttributeError as e:
            logger.error("LXMF message missing expected attributes: %s", e, exc_info=True)
        except (OSError, ValueError, KeyError) as e:
            logger.error("RNS/LXMF error processing message: %s", e, exc_info=True)
        except Exception as e:
            logger.error("Unexpected error in handle_lxmf_delivery: %s", e, exc_info=True)

    def _send_message(self, dest_identity, content):
        """Build and send a protobuf-encoded TextMessage to the given
        destination identity via LXMF opportunistic delivery.

        Args:
            dest_identity: RNS.Identity of the destination node (wrapped into
                a ``RNS.Destination`` internally for ``LXMessage``).
            content: Text string to send.

        Returns:
            bool: True if the message was submitted successfully, False otherwise.
        """
        if not content.strip():
            logger.warning("Cannot send empty message.")
            print("Error: Message content cannot be empty.")
            return False

        if self.router is None or self.client_identity is None:
            logger.error("Cannot send — router or identity not initialised.")
            print("Error: Client not fully initialised.")
            return False

        try:
            source_hash = RNS.hexrep(self.client_identity.hash, delimit=False)

            # Build protobuf envelope with TextMessage
            envelope = LMAOEnvelope()
            envelope.text.node_id = source_hash
            envelope.text.content = content
            envelope.text.timestamp = int(time.time() * 1000)

            outbound_msg = LXMF.LXMessage(
                destination=_identity_to_destination(dest_identity),
                source=_identity_to_destination(self.client_identity),
                content=envelope.SerializeToString(),
                title="p:Envelope",
                desired_method=LXMF.LXMessage.OPPORTUNISTIC,
            )
            self.router.handle_outbound(outbound_msg)

            dest_hash = RNS.hexrep(dest_identity.hash, delimit=False)
            logger.info("Message sent to %s (%d bytes)", dest_hash, len(content))
            print(f"Sent to {dest_hash}: {content}")
            return True

        except (OSError, ValueError, KeyError) as e:
            logger.error("Failed to send message: %s", e, exc_info=True)
            print(f"Error sending message: {e}")
            return False
        except OSError as e:
            logger.error("Unexpected error sending message: %s", e, exc_info=True)
            print(f"Unexpected error: {e}")
            return False

    @staticmethod
    def _validate_hash(dest_str):
        """Validate a destination hash string.

        Must be a hex string of the correct length for a Reticulum identity
        hash (16 bytes = 32 hex chars).
        """
        if not dest_str:
            return False, "Destination hash cannot be empty."
        try:
            int(dest_str, 16)
        except ValueError:
            return False, "Destination hash must be a valid hex string."
        if len(dest_str) != 32:
            return (
                False,
                f"Destination hash must be 32 hex characters (got {len(dest_str)}).",
            )
        return True, None

    @staticmethod
    def _resolve_identity(hex_str):
        """Try to recall a RNS.Identity from a hex hash string.

        Returns the identity or None on failure (logs the error).
        """
        try:
            identity = RNS.Identity.recall(bytes.fromhex(hex_str))
        except (OSError, ValueError, KeyError) as e:
            logger.error("Failed to recall identity for %s: %s", hex_str, e, exc_info=True)
            return None
        if identity is None:
            logger.error("Identity recall returned None for %s", hex_str)
        return identity

    @staticmethod
    def _print_help():
        """Print available commands."""
        print("\nAvailable commands:")
        print("  /send <dest_hash> <message>  — Send a message to a destination")
        print("  /dest <hash>                  — Set default destination hash")
        print("  /help                         — Show this help")
        print("  /quit, /exit                  — Shut down gracefully")
        print("\nWhen a default destination is set, typing a message directly")
        print("(without a /send prefix) will send it to that destination.\n")

    def _parse_input(self, user_input):
        """Parse user input and dispatch commands.

        Args:
            user_input: Raw string from the REPL prompt.

        Returns:
            bool: False if the client should exit, True otherwise.
        """
        stripped = user_input.strip()

        if not stripped:
            return True

        # /quit or /exit
        if stripped in ("/quit", "/exit"):
            return False

        # /help
        if stripped == "/help":
            self._print_help()
            return True

        # /dest <hash>
        if stripped.startswith("/dest"):
            parts = stripped.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                print("Usage: /dest <hex_hash>")
                return True
            dest_str = parts[1].strip()
            valid, err = self._validate_hash(dest_str)
            if not valid:
                print(f"Invalid hash: {err}")
                return True
            self._default_dest_hash = dest_str
            self._default_dest_identity = self._resolve_identity(dest_str)
            if self._default_dest_identity is not None:
                print(f"Default destination set to: {dest_str}")
            else:
                print(
                    f"Warning: Could not resolve destination identity for {dest_str}. "
                    f"Hash saved, but send may fail until the identity is discoverable."
                )
            return True

        # /send <dest_hash> <message>
        if stripped.startswith("/send"):
            parts = stripped.split(maxsplit=2)
            if len(parts) < 3:
                print("Usage: /send <dest_hash> <message>")
                return True
            dest_str = parts[1].strip()
            content = parts[2].strip()
            valid, err = self._validate_hash(dest_str)
            if not valid:
                print(f"Invalid hash: {err}")
                return True
            if not content:
                print("Error: Message content cannot be empty.")
                return True
            dest_identity = self._resolve_identity(dest_str)
            if dest_identity is None:
                print(
                    f"Error: Could not resolve destination {dest_str}. Have you heard from this node?"
                )
                return True
            self._send_message(dest_identity, content)
            return True

        # Plain text — send to default destination if set
        if self._default_dest_hash:
            if self._default_dest_identity is None:
                self._default_dest_identity = self._resolve_identity(self._default_dest_hash)
                if self._default_dest_identity is None:
                    print(
                        f"Error: Could not resolve default destination {self._default_dest_hash}."
                    )
                    return True
            self._send_message(self._default_dest_identity, stripped)
        else:
            print("No default destination set. Use /dest <hash> to set one, or /send <hash> <msg>.")

        return True

    def start(self):
        """Initialize Reticulum, LXMF router, and enter interactive REPL loop."""
        # Configure logging
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
        )

        cfg_dict = self._config_dict if self._config_dict is not None else config.get_config_dict()
        rnode_port = cfg_dict["interfaces"]["RNode LoRa"]["port"]

        # Warn if RNode port is missing (but DO NOT exit)
        warn_if_rnode_missing(rnode_port, role="client")

        # Use shared init helper
        self.client_identity, self.router = init_rns_and_lxmf(
            rnode_port=rnode_port,
            configdir_factory=config.get_configdir,
            identity_storage_path="/tmp/lmao_human_client_lxmf",
            register_delivery_callback=lambda r: r.register_delivery_callback(
                self.handle_lxmf_delivery
            ),
            rnode_exists=os.path.exists(rnode_port),
        )
        identity_hex = RNS.hexrep(self.client_identity.hash, delimit=False)

        # Announce presence on the network
        try:
            self.router.announce()
            logger.info("Announcement sent.")
        except (OSError, ValueError, KeyError) as e:
            logger.warning("Failed to announce presence: %s", e, exc_info=True)

        # Print startup banner
        rnode_status = (
            f"RNode on {rnode_port}"
            if os.path.exists(rnode_port)
            else "⚠️  RNode not connected — WiFi only"
        )
        print(f"\n{'=' * 50}")
        print("LMAO Human Client — Ready")
        print(f"Node identity: {identity_hex}")
        print(f"  LoRa: {rnode_status}")
        print("  WiFi: AutoInterface enabled")
        print("  Title discriminator: p:Envelope")
        print(f"{'=' * 50}")
        print("Type /help for commands, /quit to exit.\n")

        # Interactive REPL loop
        try:
            while True:
                # Use input() for blocking user input.
                # Incoming messages are delivered via the LXMF callback in a
                # background thread and printed with a newline prefix so they
                # appear above the prompt.
                try:
                    prompt = (
                        "> "
                        if self._default_dest_hash is None
                        else f"[→{self._default_dest_hash[:8]}…]> "
                    )
                    user_input = input(prompt)
                except EOFError:
                    print("\nShutting down...")
                    break

                if not self._parse_input(user_input):
                    print("\nShutting down...")
                    break

        except KeyboardInterrupt:
            print("\nShutting down...")


def main():
    """Thin entry point: creates a Client instance and starts it."""
    Client(config.get_config_dict()).start()


if __name__ == "__main__":
    main()
