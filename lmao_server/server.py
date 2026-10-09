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
import random
import threading
import time

from google.protobuf.message import DecodeError

from lma_core import LMAOEnvelope
from lma_core.contact_book import ContactBook
from lma_core.contact_book_pg import (
    backfill_sqlite_to_pg,
    compose_postgres_dsn,
    open_contact_book,
)
from lma_core.message_utils import decode_lmao_message
from lma_core.rns_di import LXMF, RNS
from lma_core.rns_init import init_rns_and_lxmf as _shared_init
from lma_core.rns_init import warn_if_rnode_missing
from lma_core.sprout_history import SproutHistory

# Local imports
from lmao_server import config

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
        add_LMAOServicer_to_server,  # noqa: F401 — accessed by tests via module attribute
    )

    GRPC_AVAILABLE = True
except ImportError:
    GRPC_AVAILABLE = False
    logger.info("gRPC not available — K8s integration features disabled.")

# ──────────────────────────────────────────────────────────────
# NATS imports (optional — gracefully degrade if unavailable)
# ──────────────────────────────────────────────────────────────
try:
    from lma_core.queue import _NATS_AVAILABLE as _NATS_PY_AVAILABLE
    from lma_core.queue import NatsQueue

    NATS_AVAILABLE = _NATS_PY_AVAILABLE
except ImportError:
    NATS_AVAILABLE = False
    logger.info("nats-py not available — NATS JetStream publishing disabled.")

# Default NATS server address — overridable via environment variable.
# When running inside Docker with --network host, "localhost" resolves
# to the host, so the K8s NATS service must be reachable via NodePort
# or the host's cluster network.
_NATS_SERVER = os.environ.get("NATS_SERVER", "nats://localhost:4222")

# ── Client allow-list (security) ────────────────────────────────────
# Accept LXMF messages ONLY from known client identities. A stranger node that
# reaches the LoRa mesh must not be able to inject into the LMAO pipeline
# (SensorReports -> NATS -> DuckDB, or CommandRequests). The matching field is
# the *sender's lxmf/delivery destination hash*, i.e. exactly what the server
# logs as ``Source ... From: <hex>`` (the OUT lxmf.delivery destination hash,
# not the raw identity hash).
#
# Whitelist values:
#   7b38fa21e75d8866c18de3da01540f2e  production Cardputer (native C firmware,
#                                     NVS-persisted identity; printed at boot
#                                     as "my lxmf/delivery hash")
#   f5f05952392627393f067df8c9eaf6c6  Sprout native client (NVS-persisted
#                                     identity; printed at boot as
#                                     "my lxmf/delivery hash")
# Extend at deploy time via env LMAO_ALLOWED_CLIENTS (comma-separated hex).
_DEFAULT_ALLOWED_CLIENTS = {
    "7b38fa21e75d8866c18de3da01540f2e",  # Cardputer (native C)
    "f5f05952392627393f067df8c9eaf6c6",  # Sprout native
}


def _build_allowed_clients() -> set:
    s = set(_DEFAULT_ALLOWED_CLIENTS)
    env = os.environ.get("LMAO_ALLOWED_CLIENTS", "")
    s.update(x.strip().lower() for x in env.split(",") if x.strip())
    return s



ALLOWED_CLIENTS = _build_allowed_clients()

# Recent Sprout soil-moisture samples, folded in from every SensorReport the
# server receives and appended to the client reply below as a DATA line so a
# client (the Cardputer) can chart it — the server holds the stream in memory,
# the ingest pod owns DuckDB, and the chart needs no extra airtime because the
# client already round-trips a message every interval.
SPROUT_HISTORY = SproutHistory()

# Central contact book (receiver directory).  Initialized in serve(); the
# downlink sends to a known contact even if its ``caps`` broadcast never lands
# (issue #151) — a registered device is definitionally on the LMAO network.
CONTACTS: "ContactBook | None" = None  # noqa: F841 — assigned in serve()
_CONTACTS_LOCK = threading.Lock()


def _learn_contact(source_hash: str) -> None:
    """Register/touch the contact book for a reporting device (issue #151).

    The server keys the directory on the sender's ``lxmf/delivery`` hash (the
    destination it addresses downlinks to).  A device is learned automatically
    the first time it reports, so it is reachable before an operator names it;
    :meth:`ContactBook.register` honors a later operator-set name/type.
    """
    book = CONTACTS
    if book is None:
        return
    delivery_hash = source_hash.lower()
    try:
        pubkey_hex = None
        try:
            known = RNS.Identity.known_destinations.get(delivery_hash)
            if known and len(known) > 1 and isinstance(known[1], bytes):
                pubkey_hex = known[1].hex()
        except Exception:
            pubkey_hex = None
        if not book.is_known(delivery_hash):
            try:
                book.register(
                    delivery_hash=delivery_hash,
                    device_type="device",
                    pubkey_hex=pubkey_hex,
                )
                logger.info("Contact book: learned new device %s", delivery_hash[:12])
            except Exception as e:
                logger.warning("Contact book: register failed for %s: %s",
                               delivery_hash[:12], e)
        else:
            book.touch(delivery_hash)
    except Exception as e:  # the directory must never break delivery
        logger.warning("Contact book update failed for %s: %s", source_hash[:12], e)

def _warn_if_rnode_missing(rnode_port):
    """Warn if the RNode port does not exist (delegates to shared helper)."""
    warn_if_rnode_missing(rnode_port, role="server")


def _init_rns_and_lxmf(rnode_port, identity_storage_path=None):
    """Initialize Reticulum + LXMF for the server (delegates to shared helper).

    The LXMF identity (and therefore the destination hash that Cardputer
    clients bake into their config.py) is stored in *identity_storage_path*.

    The default is ``~/.local/share/lmao_server/lxmf`` — a per-user location
    that survives reboots (unlike ``/tmp`` which is wiped on restart).  A
    stale destination hash on the Cardputer causes every LXMF send to fail
    silently ("no path"), so keeping the server identity stable across
    reboots is critical for reliable LoRa mesh operation.

    The path can be overridden via the *identity_storage_path* parameter or
    the ``LMAO_SERVER_IDENTITY_PATH`` environment variable.
    """
    if identity_storage_path is None:
        identity_storage_path = os.environ.get(
            "LMAO_SERVER_IDENTITY_PATH",
            os.path.expanduser("~/.local/share/lmao_server/lxmf"),
        )
    # Ensure the parent directory exists.
    parent = os.path.dirname(identity_storage_path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except PermissionError:
            logger.warning(
                "Cannot create identity storage directory %s — "
                "the LXMF router may fail if the path does not exist.",
                parent,
            )

    return _shared_init(
        rnode_port=rnode_port,
        configdir_factory=config.get_configdir,
        identity_storage_path=identity_storage_path,
        display_name="lmao-server",
    )


def _print_startup_banner(identity_hex, rnode_port, grpc_available, nats_connected=False, delivery_hash_hex=None):
    """Print the server startup banner with identity and status info."""
    rnode_status = (
        f"RNode on {rnode_port}"
        if os.path.exists(rnode_port)
        else "⚠️  RNode not connected — LoRa unavailable"
    )
    nats_status = f"NATS: {_NATS_SERVER}" if nats_connected else "NATS: disconnected"
    print(f"\n{'=' * 50}")
    print("LMAO Server — Running (async mode)")
    print(f"Node identity: {identity_hex}")
    if delivery_hash_hex:
        # This is the value clients must set as DEST_HASH — NOT the identity.
        print(f"Delivery destination (client DEST_HASH): {delivery_hash_hex}")
    print("Listening for LXMF messages...")
    print(f"  LoRa: {rnode_status}")
    print("  WiFi: AutoInterface enabled")
    print("  Title discriminator: p:Envelope")
    print(f"  {nats_status}")
    if grpc_available:
        print("  gRPC: 0.0.0.0:50051")
    print(f"{'=' * 50}\n")


_NATS_SUBJECT = "lmao.messages.env"
_NATS_STREAM = "LMAO_MESSAGES"
_NATS_STREAM_SUBJECTS = ["lmao.messages.>"]

# Supervisor backoff for recreating a permanently closed NATS client
# (issue #85).  Exponential backoff with jitter, capped, so a burst of
# incoming messages during an outage doesn't trigger a reconnect storm.
_NATS_RECONNECT_BACKOFF_BASE = 5.0  # seconds
_NATS_RECONNECT_BACKOFF_MAX = 300.0  # seconds


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


def _announce_delivery_destinations(router, attached_interface=None):
    """Announce every registered delivery destination for LoRa path discovery.

    Single source of truth for both the startup announce and the periodic
    re-announce (issue #134).  LXMF's ``router.announce()`` requires the
    *delivery destination hash* (keyed in ``router.delivery_destinations``),
    NOT the raw identity hash — passing the identity hash is a silent no-op.

    ``attached_interface`` scopes the announce to a single RNS interface
    (e.g. the WiFi AutoInterface) when given — used by the periodic wifi-only
    re-announce so fresh AutoInterface clients can resolve the server
    identity without paying LoRa airtime (see _start_wifi_announce_loop).
    """
    for dest_hash in list(router.delivery_destinations):
        router.announce(dest_hash, attached_interface=attached_interface)
    logger.info(
        "Server announce sent (%d delivery destinations).",
        len(router.delivery_destinations),
    )


def _start_wifi_announce_loop(router):
    """Periodically re-announce delivery destinations over the home-network
    interfaces (everything except the LoRa RNode), every LMAO_ANNOUNCE_INTERVAL
    seconds.

    The startup announce is the ONLY discoverability beacon the server sends
    (issue #142, to save LoRa airtime), so a wifi client that connects to the
    TCP home-network interface after the server has booted would never learn
    the server's delivery identity to address it.  Repeating the announce per
    interface keeps fresh wifi/human clients addressable as soon as they
    connect — at negligible unicast/multicast cost on the local wifi, while
    #142's LoRa airtime rationale is preserved (LoRa is never announced here).

    Interval comes from ``LMAO_ANNOUNCE_INTERVAL`` (seconds); 0/unset keeps
    the old #142 behaviour (startup announce only).
    """
    try:
        interval = float(os.environ.get("LMAO_ANNOUNCE_INTERVAL", "0"))
    except ValueError:
        logger.warning("Invalid LMAO_ANNOUNCE_INTERVAL — disabling wifi re-announce.")
        return
    if interval <= 0:
        logger.info("WiFi re-announce disabled (LMAO_ANNOUNCE_INTERVAL unset/0).")
        return

    def announce_home_loop():
        while True:
            time.sleep(interval)
            for iface in RNS.Transport.interfaces:
                if type(iface).__name__ == "RNodeInterface":
                    continue  # #142: never spend LoRa airtime on re-announces
                try:
                    _announce_delivery_destinations(router, attached_interface=iface)
                except Exception as e:
                    logger.warning("Home-network re-announce failed on %s: %s", iface, e)

    t = threading.Thread(target=announce_home_loop, name="wifi-announce", daemon=True)
    t.start()
    logger.info("Home-network re-announce every %ss (non-LoRa interfaces).", interval)


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
        # NATS reconnect supervisor state (issue #85)
        self._nats_reconnect_lock = None  # created lazily on the server loop
        self._nats_reconnect_failures = 0
        self._nats_next_reconnect_at = 0.0

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

    def send_command(self, target_identity_hex, action, params=None, seq=None,
                     timeout_ms=60000):
        """Send a CommandRequest to a target node over LXMF (issue #78).

        Builds ``LMAOEnvelope{ request.command, seq, request_ack }``, resolves the
        target RNS.Identity, and dispatches via the LXMF router. The target
        replies a DeliveryAck echoing ``seq`` (command results land on the same
        unified ack payload).

        Args:
            target_identity_hex: Target node's identity hash as hex string.
                Empty string or ``None`` = broadcast to all nodes.
            action: Command action string (e.g. ``"reboot"``).
            params: Optional dict of string→string parameters.
            seq: Optional uint32 correlation (auto-derived if ``None``).
            timeout_ms: Retained for signature compatibility; command expiry is
                no longer encoded (the old ``expires_ms`` field is removed).

        Returns:
            ``True`` if the message was queued for delivery, ``False`` on failure.
        """
        if self.router is None:
            logger.error("send_command: router not initialised")
            return False

        now_ms = int(time.time() * 1000)
        if seq is None:
            seq = now_ms & 0xFFFFFFFF

        if params is None:
            params = {}

        # Build Request{command} + envelope control
        from lma_core import CommandRequest, LMAOEnvelope  # noqa: F811

        cmd = CommandRequest()
        cmd.target = target_identity_hex or ""
        cmd.action = action
        cmd.issued_ms = now_ms
        for k, v in params.items():
            cmd.params[k] = v

        envelope = LMAOEnvelope()
        envelope.seq = seq
        envelope.request_ack = True
        envelope.request.command.CopyFrom(cmd)

        # Resolve target identity.  Reticulum can only encrypt to a peer
        # whose public keys were previously learned (via an announce), so
        # look the identity up in the local cache with Identity.recall().
        if target_identity_hex:
            try:
                dest_identity = RNS.Identity.recall(bytes.fromhex(target_identity_hex))
                if dest_identity is None:
                    logger.error(
                        "send_command: unknown target identity %s — "
                        "no announce received from this node since server start",
                        target_identity_hex[:16],
                    )
                    return False
                dest = _identity_to_destination(dest_identity)
            except (ValueError, TypeError, KeyError) as e:
                logger.error(
                    "send_command: invalid target identity %s: %s",
                    target_identity_hex[:16], e,
                )
                return False
        else:
            # Broadcast: no specific destination
            dest = None

        # Dispatch via LXMF router
        try:
            lxmf_msg = LXMF.LXMessage(
                destination=dest,
                source=_identity_to_destination(self.server_identity),
                content=envelope.SerializeToString(),
                title="p:Envelope",
                desired_method=LXMF.LXMessage.OPPORTUNISTIC,
            )
            self.router.handle_outbound(lxmf_msg)
            logger.info(
                "CommandRequest seq=%s dispatched: action=%s target=%s",
                seq, action, target_identity_hex or "<broadcast>",
            )
            return True
        except (OSError, ValueError, KeyError, AttributeError) as e:
            logger.error("send_command: dispatch failed: %s", e, exc_info=True)
            return False

    def _send_lxmf_reply(self, source_dest, envelope):
        """Address only what the requester explicitly asked for (decoupled):
        - ``envelope.request_ack``        -> DeliveryAck echoing ``envelope.seq``
        - ``envelope.request.history``    -> ChartBundle (count/since/series honored)
        A plain sensor/text payload draws no reply — reporting is one-way.
        """
        if self.router is None:
            return
        replied = False

        def send(env):
            msg = LXMF.LXMessage(
                destination=source_dest,
                source=_identity_to_destination(self.server_identity),
                content=env.SerializeToString(),
                title="p:Envelope",
                desired_method=LXMF.LXMessage.OPPORTUNISTIC,
            )
            self.router.handle_outbound(msg)

        has_request = envelope.HasField("request")
        if has_request and envelope.request.HasField("history"):
            hist = envelope.request.history
            chart = SPROUT_HISTORY.chart_bundle(
                count=hist.count, since_ms=hist.since_ms, series=set(hist.series)
            )
            if chart is not None:
                chart_env = LMAOEnvelope()
                chart_env.chart.CopyFrom(chart)
                send(chart_env)
                logger.info("Reply sent (ChartBundle, %d samples).", len(chart.soil))
                replied = True
            else:
                logger.info("No chart history matches the request — no chart reply.")

        if envelope.request_ack:
            ack = LMAOEnvelope()
            ack.ack.seq = envelope.seq
            ack.ack.server_ms = int(time.time() * 1000)
            ack.ack.success = True
            ack.ack.node_id = self.server_identity.hexhash if hasattr(
                self.server_identity, "hexhash") else ""
            send(ack)
            logger.info("DeliveryAck sent (seq=%s).", envelope.seq)
            replied = True

        if not replied:
            logger.debug("No reply requested for this message.")

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
            # get_source() returns the sender's RNS.Destination (an OUT
            # lxmf.delivery destination in LXMF >= 1.0.1), NOT an Identity.
            # It must be used directly as the reply destination — wrapping it
            # in another RNS.Destination raises TypeError ("Invalid material
            # supplied for destination hash calculation").
            source_dest = message.get_source()
            source_hash = (
                RNS.hexrep(source_dest.hash, delimit=False) if source_dest else "<unknown>"
            )

            # Security gate: only allow known client identities (see the
            # ALLOWED_CLIENTS allow-list at module top). Drop everyone else
            # silently-ish (a loud log, but no reply, no NATS publish, no gRPC
            # fan-out), so a stranger node cannot inject into the pipeline.
            if source_hash.lower() not in ALLOWED_CLIENTS:
                logger.warning(
                    "Dropping LXMF from unauthorized node %s (not in allow-list) — "
                    "add its lxmf/delivery hash to ALLOWED_CLIENTS or LMAO_ALLOWED_CLIENTS.",
                    source_hash,
                )
                return
            # ── Contact book (issue #151): remember this device so the server
            # can send it downlinks even before / without a caps broadcast. The
            # book is the source of truth for "who is on the LMAO network".
            _learn_contact(source_hash)
            content_bytes = message.content if hasattr(message, "content") else b""
            title = message.title_as_string() if hasattr(message, "title_as_string") else ""

            logger.info(
                "Message received — From: %s  Title: %s  Content length: %d bytes",
                source_hash,
                title,
                len(content_bytes),
            )

            # Fold Sprout telemetry into the chart buffer.  A message that is
            # not a SensorReport (or an undecodable payload) must never disturb
            # the ACK path below.
            try:
                envelope = LMAOEnvelope()
                envelope.ParseFromString(content_bytes)
                if envelope.WhichOneof("payload") == "sensor":
                    SPROUT_HISTORY.update(envelope.sensor)
            except Exception:
                logger.debug("No SensorReport in this message — chart buffer unchanged")

            # Decode content (protobuf first, UTF-8 fallback, byte-count placeholder)
            decode_lmao_message(content_bytes)

            if source_dest is not None and self.router is not None:
                # Decoupled reply: only address what was explicitly requested.
                # request_ack -> DeliveryAck; request.history -> ChartBundle;
                # a plain sensor payload draws no reply (reporting is one-way).
                self._send_lxmf_reply(source_dest, envelope)
            else:
                logger.warning("Could not send reply (no source destination or router).")

            # Publish to NATS JetStream (fire-and-forget from sync context)
            if self._nats_queue is not None and self._loop is not None:
                fut = asyncio.run_coroutine_threadsafe(
                    self._publish_to_nats(source_hash, content_bytes),
                    self._loop,
                )
                fut.add_done_callback(
                    lambda f: (
                        logger.error("NATS publish task failed: %s", f.exception(), exc_info=f.exception())
                        if f.exception()
                        else None
                    )
                )
            else:
                logger.debug("NATS unavailable — skipping publish")

            # Fan out to gRPC subscribers (if any)
            self._fanout_to_grpc_subscribers(message)

        except AttributeError as e:
            logger.error("LXMF message missing expected attributes: %s", e, exc_info=True)
        except (OSError, ValueError, KeyError) as e:
            logger.error("RNS/LXMF error processing message: %s", e, exc_info=True)
        except Exception as e:
            logger.error("Unexpected error in handle_lxmf_delivery: %s", e, exc_info=True)

    async def _publish_to_nats(self, source_hash: str, content_bytes: bytes) -> None:
        """Publish an incoming LXMF message payload to NATS JetStream.

        Called fire-and-forget from the sync ``handle_lxmf_delivery``
        callback via ``asyncio.run_coroutine_threadsafe``.

        If the publish fails and the NATS client is permanently closed
        (reconnect attempts exhausted), the connection is recreated via
        the supervisor (:meth:`_ensure_nats_connected`) and the publish is
        retried once.  No failure path raises — a dead NATS must never
        crash the LXMF delivery handler (issue #85).

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
            return
        except asyncio.CancelledError:
            logger.debug("NATS publish cancelled during shutdown — skipping")
            raise
        except Exception:
            logger.warning("NATS publish failed", exc_info=True)

        # Supervisor: recreate a permanently closed client (belt-and-braces
        # on top of NatsQueue's infinite background reconnect) and retry
        # the publish once.
        try:
            reconnected = await self._ensure_nats_connected()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("NATS reconnect attempt failed", exc_info=True)
            reconnected = False
        if not reconnected:
            return
        try:
            ack = await self._nats_queue.publish(_NATS_SUBJECT, content_bytes)
            logger.info(
                "Published %d bytes from %s to NATS after reconnect (seq=%s)",
                len(content_bytes),
                source_hash,
                ack.seq,
            )
        except asyncio.CancelledError:
            logger.debug("NATS publish cancelled during shutdown — skipping")
            raise
        except Exception:
            logger.warning("NATS publish failed again after reconnect", exc_info=True)

    async def _ensure_nats_connected(self) -> bool:
        """Recreate the NATS connection if the client is permanently closed.

        ``NatsQueue.connect()`` is configured with infinite reconnect
        attempts, so nats-py normally recovers outages on its own in the
        background.  This supervisor is the fallback for when the client
        has fully given up (``is_closed``) — it rebuilds the connection
        and re-ensures the JetStream stream, with exponential backoff +
        jitter so a burst of messages during an outage doesn't cause a
        reconnect storm (issue #85).

        Returns:
            True if the queue is connected and ready to publish.
        """
        queue = self._nats_queue
        if queue is None:
            return False
        if not queue.is_closed:
            # Connected, or nats-py is reconnecting in the background —
            # leave it alone; publishing resumes by itself once reconnected.
            return queue.is_connected
        if self._nats_reconnect_lock is None:
            self._nats_reconnect_lock = asyncio.Lock()
        async with self._nats_reconnect_lock:
            # Re-check under the lock — another publish task may have
            # reconnected while we were waiting.
            if not queue.is_closed:
                return queue.is_connected
            now = time.monotonic()
            if now < self._nats_next_reconnect_at:
                logger.debug(
                    "NATS reconnect backoff active (%.1fs remaining) — skipping attempt",
                    self._nats_next_reconnect_at - now,
                )
                return False
            logger.warning(
                "NATS client permanently closed — recreating connection to %s",
                _NATS_SERVER,
            )
            try:
                await queue.connect(servers=_NATS_SERVER)
                await queue.ensure_stream(_NATS_STREAM, _NATS_STREAM_SUBJECTS)
            except Exception:
                self._nats_reconnect_failures += 1
                backoff = min(
                    _NATS_RECONNECT_BACKOFF_BASE * (2 ** (self._nats_reconnect_failures - 1)),
                    _NATS_RECONNECT_BACKOFF_MAX,
                )
                # Up to 25% jitter to avoid lock-step reconnect storms
                backoff += random.uniform(0, backoff * 0.25)
                self._nats_next_reconnect_at = now + backoff
                logger.warning(
                    "NATS reconnect failed (attempt %d) — next attempt in %.1fs",
                    self._nats_reconnect_failures,
                    backoff,
                    exc_info=True,
                )
                return False
            logger.info("NATS connection re-established — publishing resumed")
            self._nats_reconnect_failures = 0
            self._nats_next_reconnect_at = 0.0
            return True


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

            # Resolve destination identity from the envelope payload.
            # SendRequest carries only the serialized envelope, so the
            # destination must come from the payload itself — currently only a
            # command request's target (envelope.request.command.target) carries
            # a destination identity. Reticulum can only encrypt to a peer whose
            # public keys were previously learned (via an announce), so look the
            # identity up in the local cache with Identity.recall().
            dest_hash = ""
            if envelope.HasField("request"):
                cmd = envelope.request.command if envelope.request.HasField(
                    "command") else None
                if cmd is not None:
                    dest_hash = cmd.target
            try:
                dest = RNS.Identity.recall(bytes.fromhex(dest_hash)) if dest_hash else None
            except (ValueError, TypeError, KeyError):
                dest = None
            if not dest:
                return SendResponse(
                    destination_hash=dest_hash,
                    status="error: invalid or unreachable destination",
                )

            # Build an LXMF message and dispatch via the router
            try:
                lxmf_msg = LXMF.LXMessage(
                    destination=_identity_to_destination(dest),
                    source=_identity_to_destination(self._server.server_identity),
                    content=envelope.SerializeToString(),
                    title="p:Envelope",
                    desired_method=LXMF.LXMessage.OPPORTUNISTIC,
                )
                self._router.handle_outbound(lxmf_msg)
                return SendResponse(
                    destination_hash=dest_hash,
                    status="queued",
                )
            except (OSError, ValueError, KeyError) as e:
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
                    except (AttributeError, OSError, ValueError, KeyError) as e:
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

    # ── Central contact book (issue #151) ─────────────────────────
    # Receiver directory. Served from the dedicated in-cluster Postgres
    # service (k8s/postgres.yaml) when LMAO_CONTACTS_URL or LMAO_CONTACTS_PG_*
    # is set (the legacy SQLite file is backfilled into it, idempotent);
    # otherwise the original SQLite file is used (dev / non-cluster runs).
    global CONTACTS

    def _default_sqlite_path() -> str:
        _id_dir = os.path.dirname(
            os.environ.get(
                "LMAO_SERVER_IDENTITY_PATH",
                os.path.expanduser("~/.local/share/lmao_server/lxmf"),
            )
        )
        return os.path.join(_id_dir, "contacts.db")

    contacts_dsn = compose_postgres_dsn()
    if contacts_dsn is not None:
        try:
            migrated = backfill_sqlite_to_pg(
                os.environ.get("LMAO_CONTACTS_DB") or _default_sqlite_path(),
                contacts_dsn,
            )
            if migrated:
                logger.info("Backfilled %d contact(s) from SQLite into Postgres", migrated)
            CONTACTS = open_contact_book(contacts_dsn)
            logger.info("Contact book ready (Postgres) at %s", contacts_dsn)
        except Exception as e:
            CONTACTS = None
            logger.warning("Contact book unavailable (%s) — downlink requires caps", e)
    else:
        contacts_db = os.environ.get("LMAO_CONTACTS_DB") or _default_sqlite_path()
        try:
            os.makedirs(os.path.dirname(contacts_db) or ".", exist_ok=True)
            CONTACTS = ContactBook(contacts_db)
            logger.info("Contact book ready at %s", contacts_db)
        except Exception as e:
            CONTACTS = None
            logger.warning("Contact book unavailable (%s) — downlink requires caps", e)

    # Create Server instance (wraps router + identity)
    lmao_server = Server(config_dict=cfg_dict)
    lmao_server.server_identity = server_identity
    lmao_server.router = router


    # Register the delivery callback
    router.register_delivery_callback(lmao_server.handle_lxmf_delivery)

    # ── Announce presence for LoRa path discovery ─────────────────
    # Clients need the server to announce so they can discover a path and
    # recall the server's identity keys.  LXMF's router.announce() requires
    # the *delivery destination hash* (keyed in router.delivery_destinations),
    # NOT the raw identity hash — passing the identity hash is a silent no-op.
    #
    # This one-shot announce on startup is the ONLY announce the server sends.
    # The periodic re-announce is retired (issue #142): native clients discover
    # the server ON DEMAND via RNS path requests (Sprout native `path_find`,
    # Cardputer µReticulum `ensure_path`), which the server answers even as a
    # leaf node (rns_init leaf-node patch, issue #135).  Since #142 the answer
    # is deferred by _PATH_REQUEST_ANSWER_DELAY so a half-duplex requester does
    # not miss it in its own TX→RX turnaround.  The startup announce below is
    # kept only for clients already listening when the server boots.
    logger.info("Announcing server presence for LoRa path discovery...")
    try:
        _announce_delivery_destinations(router)
    except Exception as e:
        logger.warning("Server announce failed (LoRa may be unavailable): %s", e)

    # ── Periodic WiFi-only re-announce (AutoInterface) ───────────
    # Fresh wifi AutoInterface clients cannot resolve the server's delivery
    # identity from the single startup announce; repeat it over the
    # AutoInterface only (LMAO_ANNOUNCE_INTERVAL) so this and future wifi
    # clients stay addressable without burning LoRa airtime (#142).
    _start_wifi_announce_loop(router)

    # ── NATS connect (optional) ─────────────────────────────────
    nats_queue = None
    if NATS_AVAILABLE:
        try:
            nats_queue = NatsQueue(name="lmao-server")
            await nats_queue.connect(servers=_NATS_SERVER)
            await nats_queue.ensure_stream(_NATS_STREAM, _NATS_STREAM_SUBJECTS)
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

    # Print banner (including the delivery destination hash clients
    # must use as DEST_HASH — it differs from the raw identity hash)
    _delivery_hashes = list(router.delivery_destinations)
    _print_startup_banner(
        RNS.hexrep(server_identity.hash, delimit=False),
        rnode_port,
        GRPC_AVAILABLE,
        nats_queue is not None,
        delivery_hash_hex=(
            RNS.hexrep(_delivery_hashes[0], delimit=False)
            if _delivery_hashes
            else None
        ),
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

    # ── Contacts API (receiver directory) ────────────────────────
    contacts_runner = None
    if CONTACTS is not None:
        try:
            from lma_core.contacts_api import start_contacts_server

            contacts_runner = await start_contacts_server(
                CONTACTS, port=int(os.environ.get("LMAO_CONTACTS_PORT", "8081"))
            )
        except Exception as e:
            logger.warning("Contacts API unavailable (%s)", e)

    # Keep running until interrupted.  Discovery is on-demand: the server
    # answers client path requests (rns_init leaf-node patch) rather than
    # re-announcing periodically (issue #142 — no announce task to track).
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
        if contacts_runner is not None:
            try:
                await contacts_runner.cleanup()
            except Exception:
                pass
        if lmao_server:
            lmao_server.clear_grpc_subscribers()
            lmao_server._nats_queue = None  # Prevent new NATS publish attempts during shutdown
        if nats_queue is not None:
            await nats_queue.close()
            logger.info("NATS connection closed.")


if __name__ == "__main__":
    # When run directly, prefer the gRPC-enabled async main
    asyncio.run(async_main())
