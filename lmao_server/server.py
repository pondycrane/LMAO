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
import hashlib
import logging
import os
import random
import threading
import time
from collections import OrderedDict

from google.protobuf.message import DecodeError

from lma_core import LMAOEnvelope
from lma_core.attachment import (
    ACK_ABORTED,
    ACK_COMPLETE,
    ACK_NEED,
    ACK_REJECTED,
    KIND_CHART,
    RETRY_MAX_ATTEMPTS,
    build_transfer,
    decode_envelope,
    retry_delay_seconds,
)
from lma_core.contact_book import ContactBook
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

# ── LMAF (LMAO Attachment Framing) — capability cache + chart delivery ───
# A peer that advertises ``KIND_CHART`` with ``lmaf_version >= 1`` gets the
# chart as an LMAF manifest + chunks instead of a ``DATA`` line folded into the
# ACK text (a DATA line must fit one opportunistic LXMF packet, ~295 B; LMAF
# lifts that limit).  Peers that do not advertise it keep the legacy reply
# byte-for-byte — the production MicroPython Cardputer must not regress.
LMAF_CHART_CODEC = "lmao:chart-line-v1"

# LoRa is half-duplex: back-to-back packets lose the sender's turnaround, so
# space LMAF packets (manifest + chunks) apart.  Same reason as the deferred
# path-request answer (lma_core/rns_init.py::_PATH_REQUEST_ANSWER_DELAY).
LMAO_LMAF_PACING_SECONDS = float(os.environ.get("LMAO_LMAF_PACING_SECONDS", "1.0"))

# Half-duplex turnaround before the first LMAF packet: the peer that just
# reported is still transmitting its own frames (report + the announce pair
# that follows), and a single radio cannot hear anything while it transmits —
# a reply sent immediately lands in that deaf window and is simply lost.  The
# same reasoning drives lma_core/rns_init.py's _PATH_REQUEST_ANSWER_DELAY.
LMAO_LMAF_TURNAROUND_SECONDS = float(os.environ.get("LMAO_LMAF_TURNAROUND_SECONDS", "1.5"))

# The manifest is the one packet a transfer cannot survive without, and it does
# not fit a single LoRa frame: send it twice (the receiver treats a repeat as a
# resume).  Chunks are covered by the NEED exchange the receiver drives.
LMAF_MANIFEST_REPEATS = int(os.environ.get("LMAO_LMAF_MANIFEST_REPEATS", "2"))

# A path can vanish between the report that triggers a transfer and the send
# itself: this is a leaf node (transport disabled), so RNS culls its path table
# and a packet handed to the router without a path is dropped ("Dropped packet
# since path table entry disappeared during outbound processing").  LXMF's
# retry loop used to paper over exactly that for the legacy reply; LMAF packets
# are sent once, so wait for the path here instead.  Waiting costs no airtime —
# nothing is transmitted while the path is missing — and the peer's announce
# (every 30 s) or the path request itself restores it.
LMAO_LMAF_PATH_WAIT_SECONDS = float(os.environ.get("LMAO_LMAF_PATH_WAIT_SECONDS", "1.0"))
LMAF_PATH_WAIT_ATTEMPTS = 3

# Pending transfers retained for NEED resends — bounded so a lossy/slow peer
# cannot grow server state without limit (oldest is evicted first).
LMAF_MAX_PENDING = 8
LMAF_MAX_RESEND_ATTEMPTS = 3

# Dead-letter retry: a transfer whose peer never replied at all (not even a
# NEED) would otherwise vanish after one attempt — tolerable for a chart the
# next report regenerates, fatal for a voice note.  After the shared policy's
# delay the manifest is re-offered (one packet, the recovery primitive), backing
# off per lma_core.attachment, then the transfer is abandoned with a warning.
# `LMAO_LMAF_MAX_RETRIES=0` disables the retry entirely.
LMAF_RETRY_MAX_ATTEMPTS = int(
    os.environ.get("LMAO_LMAF_MAX_RETRIES", str(RETRY_MAX_ATTEMPTS))
)
# How often the retry task looks at the pending set.  Only the timer resolution
# — the schedule itself is the policy above.
LMAF_RETRY_TICK_SECONDS = float(os.environ.get("LMAO_LMAF_RETRY_TICK_SECONDS", "5.0"))

# Dead-letter re-sends re-transmit the *identical* envelope, so the content
# bytes are the identity of a message; genuinely new telemetry differs.  A
# duplicate must not double-count (chart buffer, ingest pipeline, subscribers)
# but must still be *answered* — that reply is the delivery proof the
# re-sending client is waiting for.  Bounded in peers and in time: this exists
# to stop double-counting, not to keep a message log.
LMAF_DEDUP_WINDOW_SECONDS = float(os.environ.get("LMAO_DEDUP_WINDOW_SECONDS", "900"))
LMAF_DEDUP_MAX_PEERS = 32


class LmafCapabilityCache:
    """Peer hash -> advertised LMAF capabilities (``caps`` envelopes).

    ``handle_lxmf_delivery`` runs on the RNS thread while the asyncio loop can
    read the cache, hence the lock.  Only allow-listed peers can populate it,
    so the map is bounded by the allow-list.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._caps = {}

    def update(self, peer_hash, decoded_caps):
        """Record a decoded ``Capability`` for *peer_hash*; returns the entry."""
        entry = {
            "lmaf_version": int(decoded_caps.get("lmaf_version", 0)),
            "kinds": list(decoded_caps.get("kinds", ())),
            "max_chunk_size": int(decoded_caps.get("max_chunk_size", 0)),
            "updated": time.time(),
        }
        with self._lock:
            self._caps[peer_hash.lower()] = entry
        return entry

    def get(self, peer_hash):
        with self._lock:
            return self._caps.get(peer_hash.lower())

    def chart_capable(self, peer_hash):
        """True when the peer can receive an LMAF chart transfer."""
        entry = self.get(peer_hash)
        return bool(
            entry and entry["lmaf_version"] >= 1 and KIND_CHART in entry["kinds"]
        )

    def clear(self):
        with self._lock:
            self._caps.clear()

    def __len__(self):
        with self._lock:
            return len(self._caps)


LMAF_CAPS = LmafCapabilityCache()

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


def _announce_delivery_destinations(router):
    """Announce every registered delivery destination for LoRa path discovery.

    Single source of truth for both the startup announce and the periodic
    re-announce (issue #134).  LXMF's ``router.announce()`` requires the
    *delivery destination hash* (keyed in ``router.delivery_destinations``),
    NOT the raw identity hash — passing the identity hash is a silent no-op.
    """
    for dest_hash in list(router.delivery_destinations):
        router.announce(dest_hash)
    logger.info(
        "Server announce sent (%d delivery destinations).",
        len(router.delivery_destinations),
    )


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
        # LMAF transfers awaiting an ack, keyed by transfer id (bounded — see
        # LMAF_MAX_PENDING).  Guarded by a lock: the RNS delivery callback and
        # the asyncio loop both touch it.
        self._lmaf_lock = threading.Lock()
        self._lmaf_pending = OrderedDict()
        # Recent content per peer, for duplicate (dead-letter re-send) suppression.
        self._dedup_lock = threading.Lock()
        self._recent_content = {}

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

    def send_command(self, target_identity_hex, action, params=None, cmd_id=None,
                     timeout_ms=60000):
        """Send a CommandRequest to a target node over LXMF (issue #78).

        Builds a protobuf CommandRequest, wraps it in an LMAOEnvelope,
        resolves the target RNS.Identity, and dispatches via the LXMF router.

        Args:
            target_identity_hex: Target node's identity hash as hex string.
                Empty string or ``None`` = broadcast to all nodes.
            action: Command action string (e.g. ``"reboot"``).
            params: Optional dict of string→string parameters.
            cmd_id: Optional command ID (auto-generated if ``None``).
            timeout_ms: Command expiry in milliseconds from now.

        Returns:
            ``True`` if the message was queued for delivery, ``False`` on failure.
        """
        if self.router is None:
            logger.error("send_command: router not initialised")
            return False

        if cmd_id is None:
            cmd_id = f"cmd-{int(time.time() * 1000)}"

        now_ms = int(time.time() * 1000)
        issued_ms = now_ms
        expires_ms = now_ms + timeout_ms

        if params is None:
            params = {}

        # Build protobuf CommandRequest
        from lma_core import CommandRequest, LMAOEnvelope  # noqa: F811

        cmd = CommandRequest()
        cmd.cmd_id = cmd_id
        cmd.target = target_identity_hex or ""
        cmd.action = action
        cmd.issued_ms = issued_ms
        cmd.expires_ms = expires_ms
        for k, v in params.items():
            cmd.params[k] = v

        # Wrap in LMAOEnvelope
        envelope = LMAOEnvelope()
        envelope.command.CopyFrom(cmd)

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
                "CommandRequest %s dispatched: action=%s target=%s",
                cmd_id, action, target_identity_hex or "<broadcast>",
            )
            return True
        except (OSError, ValueError, KeyError, AttributeError) as e:
            logger.error("send_command: dispatch failed: %s", e, exc_info=True)
            return False

    def _is_duplicate(self, source_hash: str, content: bytes) -> bool:
        """True when these exact bytes already arrived from this peer recently.

        A client that gets no reply re-transmits the same envelope (the
        dead-letter queue), and that repeat is byte-identical while new
        telemetry is not.
        """
        digest = hashlib.sha256(content).digest()
        now = time.monotonic()
        with self._dedup_lock:
            previous = self._recent_content.get(source_hash)
            self._recent_content[source_hash] = (digest, now)
            if len(self._recent_content) > LMAF_DEDUP_MAX_PEERS:
                oldest = min(self._recent_content, key=lambda k: self._recent_content[k][1])
                self._recent_content.pop(oldest, None)
        return bool(
            previous is not None
            and previous[0] == digest
            and now - previous[1] < LMAF_DEDUP_WINDOW_SECONDS
        )

    def handle_lxmf_delivery(self, message):
        """Decodes incoming content as a protobuf LMAOEnvelope. The protocol uses
        title="p:Envelope" as a convention, but the handler attempts protobuf
        decode unconditionally and falls back to raw UTF-8 text for backward
        compatibility with non-protobuf senders. Sends a protobuf-encoded
        TextMessage ACK as a reply.

        Also publishes the incoming message payload to NATS JetStream
        (fire-and-forget via asyncio.run_coroutine_threadsafe) and fans out
        to gRPC subscribers so streaming clients receive the message.

        LMAF (LMAO Attachment Framing): a peer that advertised ``KIND_CHART``
        via a ``caps`` envelope gets the chart as a paced manifest + chunk
        transfer instead of a ``DATA`` line folded into the ACK text.  LMAF
        control envelopes (caps/ack, and inbound manifest/chunk) are handled as
        protocol traffic and are not answered — see :meth:`_handle_lmaf_ack`.
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

            # Decode once, early: the payload kind decides whether a repeat is a
            # duplicate *delivery* or merely idempotent protocol traffic.  A
            # capability re-advertisement is byte-identical by nature (the
            # device retries it until the server proves it understood), so
            # flagging it as a duplicate would be wrong — and would read as a
            # fault in the logs.  Handling stays below; this only classifies.
            lmaf_decoded = decode_envelope(content_bytes)
            lmaf_kind = lmaf_decoded.get("payload") if lmaf_decoded is not None else None
            is_control = lmaf_kind in ("capability", "ack", "manifest", "chunk")

            # A dead-letter re-send of telemetry or an attachment is
            # byte-identical to the original: skip its side effects, but still
            # answer it below (that answer is the proof the re-sending client is
            # waiting for).
            duplicate = False if is_control else self._is_duplicate(source_hash, content_bytes)
            if duplicate:
                logger.info(
                    "Duplicate from %s (byte-identical re-send) — ingestion skipped, still replying",
                    source_hash,
                )

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
                if envelope.WhichOneof("payload") == "sensor" and not duplicate:
                    SPROUT_HISTORY.update(envelope.sensor)
            except Exception:
                logger.debug("No SensorReport in this message — chart buffer unchanged")

            # ── LMAF control traffic ───────────────────────────────────
            # A caps envelope teaches us what this peer can receive; an ack
            # drives resend/teardown of a pending chart transfer.  Inbound
            # manifest/chunk reassembly is not a server concern (the server is
            # the attachment sender), so those are ignored.
            if lmaf_decoded is not None:
                lmaf_kind = lmaf_decoded.get("payload")
                if lmaf_kind == "capability":
                    entry = LMAF_CAPS.update(source_hash, lmaf_decoded)
                    logger.info(
                        "LMAF caps from %s: version=%d kinds=%s max_chunk=%d",
                        source_hash,
                        entry["lmaf_version"],
                        entry["kinds"],
                        entry["max_chunk_size"],
                    )
                elif lmaf_kind == "ack":
                    self._handle_lmaf_ack(lmaf_decoded)
                elif lmaf_kind in ("manifest", "chunk"):
                    logger.debug(
                        "LMAF inbound %s id=%s (server does not reassemble)",
                        lmaf_kind,
                        lmaf_decoded.get("id", "")[:8],
                    )

            if is_control:
                # Control traffic is not a report and must not be answered: a
                # chart reply would make the peer's own chunk acks loop back
                # into fresh chart transfers.  It is not telemetry either, so it
                # is not published to the ingest pipeline.  gRPC subscribers
                # still see every accepted message.
                self._fanout_to_grpc_subscribers(message)
                return

            # Decode content (protobuf first, UTF-8 fallback, byte-count placeholder)
            decode_lmao_message(content_bytes)

            # Build and send a protobuf-encoded ACK reply
            reply_text = (
                f"ACK from LMAO Server — received your message ({len(content_bytes)} bytes)"
            )
            chart_line = SPROUT_HISTORY.data_line()
            lmaf_peer = (
                LMAF_CAPS.chart_capable(source_hash)
                or (CONTACTS is not None and CONTACTS.is_known(source_hash))
            )
            if chart_line and lmaf_peer:
                # An LMAF peer is not bound by the single-packet DATA-line cap,
                # so it gets the full air rings — the depth this framing exists
                # to carry (the line is sent as manifest + chunks, never folded
                # into the ACK text).
                chart_line = SPROUT_HISTORY.data_line(air_limit=None)
            elif chart_line:
                # Piggyback the chart payload on the reply the client already
                # solicits — no extra frames, no query protocol.
                reply_text = f"{reply_text}\n{chart_line}"
            logger.info("Reply: %s", reply_text)

            if source_dest is not None and self.router is not None:
                # An LMAF peer acknowledges the transfer itself, so the ACK text
                # is pure overhead — and worse, it would be transmitted straight
                # into the window where the peer is still finishing its own
                # report/announce burst and cannot hear anything (half duplex,
                # one radio).  Skip it; the transfer below is the reply.
                send_ack_text = not lmaf_peer
                if send_ack_text:
                    reply_envelope = LMAOEnvelope()
                    reply_envelope.text.node_id = source_hash
                    reply_envelope.text.content = reply_text
                    reply_envelope.text.timestamp = int(time.time() * 1000)

                    reply_msg = LXMF.LXMessage(
                        destination=source_dest,
                        source=_identity_to_destination(self.server_identity),
                        content=reply_envelope.SerializeToString(),
                        title="p:Envelope",
                        desired_method=LXMF.LXMessage.OPPORTUNISTIC,
                    )
                    self.router.handle_outbound(reply_msg)
                    logger.info("Reply sent.")

                if chart_line and lmaf_peer:
                    # Chart too large for one opportunistic packet — send it as
                    # an LMAF transfer, paced for the half-duplex LoRa link.
                    self._start_lmaf_chart(source_dest, source_hash, chart_line.encode("utf-8"))
            else:
                logger.warning("Could not send reply (no source destination or router).")

            # Publish to NATS JetStream (fire-and-forget from sync context)
            if duplicate:
                pass  # accepted and answered, but not re-ingested
            elif self._nats_queue is not None and self._loop is not None:
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

            # Fan out to gRPC subscribers (if any) — not for re-sends: they
            # carry no new content, they only prove delivery.
            if not duplicate:
                self._fanout_to_grpc_subscribers(message)

        except AttributeError as e:
            logger.error("LXMF message missing expected attributes: %s", e, exc_info=True)
        except (OSError, ValueError, KeyError) as e:
            logger.error("RNS/LXMF error processing message: %s", e, exc_info=True)
        except Exception as e:
            logger.error("Unexpected error in handle_lxmf_delivery: %s", e, exc_info=True)

    # ── LMAF transfer state + sending ───────────────────────────────

    def _lmaf_remember(self, transfer, dest, source_hash):
        """Track a sent transfer for NEED resends, evicting the oldest beyond
        :data:`LMAF_MAX_PENDING`."""
        with self._lmaf_lock:
            self._lmaf_pending[transfer.id] = {
                "transfer": transfer,
                "dest": dest,
                "source_hash": source_hash,
                "attempts": 0,
                # Dead-letter bookkeeping: when this transfer last saw any sign
                # of life from the peer, and how many timer-driven re-offers it
                # has had.  Deliberately separate from the NEED-driven `attempts`
                # above: a peer that keeps asking for parts is making progress
                # and must never be abandoned by the timer.
                "sent_at": time.monotonic(),
                "retries": 0,
            }
            self._lmaf_pending.move_to_end(transfer.id)
            while len(self._lmaf_pending) > LMAF_MAX_PENDING:
                evicted, _ = self._lmaf_pending.popitem(last=False)
                logger.info(
                    "LMAF evicting pending transfer id=%s (pending cap %d)",
                    evicted[:8],
                    LMAF_MAX_PENDING,
                )

    def _lmaf_forget(self, id_hex):
        with self._lmaf_lock:
            return self._lmaf_pending.pop(id_hex, None)

    def _lmaf_touch(self, id_hex):
        """Record that the peer said something about this transfer.

        Any inbound ack restarts the dead-letter clock: a peer naming missing
        chunks, reporting progress, or rejecting the transfer is alive and
        driving the exchange, so the timer must not give up on it.
        """
        with self._lmaf_lock:
            entry = self._lmaf_pending.get(id_hex)
            if entry is not None:
                entry["sent_at"] = time.monotonic()

    def _start_lmaf_chart(self, dest, source_hash, chart_payload):
        """Frame the chart payload as an LMAF transfer and schedule its packets."""
        try:
            transfer = build_transfer(
                chart_payload,
                kind=KIND_CHART,
                codec=LMAF_CHART_CODEC,
                node_id=source_hash,
            )
        except Exception:
            logger.error("LMAF: could not build the chart transfer — chart not sent", exc_info=True)
            return
        self._lmaf_remember(transfer, dest, source_hash)
        logger.info(
            "LMAF send id=%s kind=%d chunks=%d bytes=%d",
            transfer.id[:8],
            KIND_CHART,
            transfer.chunk_count,
            transfer.total_bytes,
        )
        self._dispatch_lmaf_packets(
            [transfer.manifest_envelope] * max(1, LMAF_MANIFEST_REPEATS)
            + list(transfer.chunk_envelopes),
            dest,
            source_hash,
        )

    def _handle_lmaf_ack(self, decoded):
        """Apply one inbound ``AttachmentAck``.

        NEED resends exactly the listed chunk indices (at most
        :data:`LMAF_MAX_RESEND_ATTEMPTS` per transfer, then the pending state is
        dropped); COMPLETE/REJECTED/ABORTED log one line each and drop the
        pending transfer.
        """
        id_hex = decoded.get("id", "")
        short = id_hex[:8]
        status = decoded.get("status", 0)

        # Any inbound ack restarts the dead-letter clock (see _lmaf_touch).
        self._lmaf_touch(id_hex)

        if status == ACK_NEED:
            self._resend_lmaf_chunks(id_hex, short, decoded.get("missing") or [])
            return

        if status in (ACK_COMPLETE, ACK_REJECTED, ACK_ABORTED):
            entry = self._lmaf_forget(id_hex)
            transfer = entry["transfer"] if entry else None
            if status == ACK_COMPLETE:
                if transfer is None:
                    logger.info("LMAF complete id=%s (no pending transfer)", short)
                else:
                    logger.info(
                        "LMAF complete id=%s bytes=%d chunks=%d",
                        short,
                        transfer.total_bytes,
                        transfer.chunk_count,
                    )
            elif status == ACK_REJECTED:
                logger.info(
                    "LMAF rejected id=%s reason=%s", short, decoded.get("reason") or "-"
                )
            else:
                logger.info(
                    "LMAF aborted id=%s reason=%s", short, decoded.get("reason") or "-"
                )
            return

        # RECEIVING is a progress report; the NEED path drives any resend.
        logger.debug(
            "LMAF progress id=%s have=%d missing=%s",
            short,
            decoded.get("have_count", 0),
            decoded.get("missing") or [],
        )

    def _resend_lmaf_chunks(self, id_hex, short, missing):
        """Resend what a peer's NEED ack asked for, bounded per transfer.

        An empty ``missing`` list is a *manifest request*: the peer holds chunks
        whose manifest never arrived, so nothing can be placed.  The whole
        transfer is resent (manifest first) — that is the only thing that can
        recover the session.
        """
        give_up = False
        with self._lmaf_lock:
            entry = self._lmaf_pending.get(id_hex)
            if entry is not None:
                attempts = entry["attempts"] + 1
                if attempts > LMAF_MAX_RESEND_ATTEMPTS:
                    del self._lmaf_pending[id_hex]
                    entry = None
                    give_up = True
                else:
                    entry["attempts"] = attempts
        if give_up:
            logger.warning(
                "LMAF giving up id=%s after %d resend attempts",
                short,
                LMAF_MAX_RESEND_ATTEMPTS,
            )
            return
        if entry is None:
            logger.info("LMAF resend asked for unknown transfer id=%s — ignored", short)
            return

        transfer = entry["transfer"]
        indices = [int(i) for i in missing]
        if not indices:
            logger.info(
                "LMAF manifest request id=%s chunks=%d — resending manifest + chunks",
                short,
                transfer.chunk_count,
            )
            packets = [transfer.manifest_envelope, *transfer.chunk_envelopes]
        else:
            packets = [
                transfer.chunk_envelopes[i] for i in indices if 0 <= i < transfer.chunk_count
            ]
        if not packets:
            logger.info("LMAF resend id=%s: no valid chunk indices in %s", short, indices)
            return
        logger.info(
            "LMAF resend id=%s chunks=%d attempt=%d", short, len(packets), entry["attempts"]
        )
        self._dispatch_lmaf_packets(packets, entry["dest"], entry["source_hash"])

    def _lmaf_retry_pass(self, now=None):
        """One dead-letter pass: re-offer — or abandon — transfers peers ignored.

        The receiver drives normal recovery (NEED for missing chunks, an empty
        NEED for a lost manifest).  What it cannot cover is a transfer the peer
        never noticed at all: every frame lost, so no ack of any kind comes back
        and the transfer would vanish after a single attempt.  Such a transfer is
        re-offered the *manifest* — one packet, and the recovery primitive, since
        a peer holding any chunk answers with a NEED — on the shared backoff
        schedule, then abandoned loudly so the loss is visible instead of silent.

        Runs from :meth:`_lmaf_retry_loop` on the asyncio loop; separated (and
        given an injectable clock) so the schedule is testable without sleeping.
        Returns the number of re-offers dispatched.
        """
        if now is None:
            now = time.monotonic()

        reoffers = []
        abandoned = []
        with self._lmaf_lock:
            for id_hex, entry in list(self._lmaf_pending.items()):
                idle = now - entry["sent_at"]
                if idle < retry_delay_seconds(entry["retries"]):
                    continue
                if entry["retries"] >= LMAF_RETRY_MAX_ATTEMPTS:
                    abandoned.append((id_hex, entry["retries"]))
                    del self._lmaf_pending[id_hex]
                    continue
                entry["retries"] += 1
                entry["sent_at"] = now
                reoffers.append(
                    (id_hex, entry["retries"], idle, entry["transfer"],
                     entry["dest"], entry["source_hash"])
                )

        # Dispatch outside the lock.  An ack may have retired the transfer in
        # the meantime, in which case the peer sees one duplicate manifest and
        # answers COMPLETE without re-downloading anything (the receiver
        # remembers delivered ids) — cheap, and safer than holding the lock
        # across a cross-thread schedule.
        for id_hex, attempt, idle, transfer, dest, source_hash in reoffers:
            logger.info(
                "LMAF re-offer id=%s attempt=%d/%d after %.0fs without an ack",
                id_hex[:8],
                attempt,
                LMAF_RETRY_MAX_ATTEMPTS,
                idle,
            )
            self._dispatch_lmaf_packets([transfer.manifest_envelope], dest, source_hash)

        for id_hex, retries in abandoned:
            logger.warning(
                "LMAF abandoned id=%s after %d re-offers (peer never acked)",
                id_hex[:8],
                retries,
            )
        return len(reoffers)

    async def _lmaf_retry_loop(self):
        """Periodic dead-letter pass (see :meth:`_lmaf_retry_pass`)."""
        while True:
            try:
                await asyncio.sleep(LMAF_RETRY_TICK_SECONDS)
                self._lmaf_retry_pass()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A retry pass must never take the server down with it.
                logger.error("LMAF dead-letter pass failed", exc_info=True)

    def _dispatch_lmaf_packets(self, packets, dest, source_hash, pacing=None):
        """Send LMAF packets, spaced by :data:`LMAO_LMAF_PACING_SECONDS`.

        ``handle_lxmf_delivery`` runs on the RNS thread, so the paced coroutine
        is scheduled on the server's asyncio loop (the same pattern as the NATS
        publish).  Without a captured loop the packets go out unpaced rather
        than being dropped.
        """
        if not packets:
            return
        if pacing is None:
            pacing = LMAO_LMAF_PACING_SECONDS
        loop = self._loop
        if loop is None:
            logger.debug(
                "LMAF: no event loop captured — sending %d packets unpaced", len(packets)
            )
            for content in packets:
                self._send_lmaf_packet(dest, source_hash, content)
            return
        asyncio.run_coroutine_threadsafe(
            self._send_lmaf_transfer(packets, dest, source_hash, pacing), loop
        )

    async def _ensure_lmaf_path(self, dest, source_hash):
        """Wait for a Reticulum path before handing a packet to the transport.

        Returns True when a path is known.  A missing path is requested and
        then polled up to :data:`LMAF_PATH_WAIT_ATTEMPTS` times; the wait costs
        no airtime (nothing is transmitted while the path is missing), whereas
        sending anyway means the transport silently discards the packet.
        """
        if RNS.Transport.has_path(dest.hash):
            return True
        for _ in range(LMAF_PATH_WAIT_ATTEMPTS):
            RNS.Transport.request_path(dest.hash)
            await asyncio.sleep(LMAO_LMAF_PATH_WAIT_SECONDS)
            if RNS.Transport.has_path(dest.hash):
                logger.info("LMAF: path to %s restored by a path request", source_hash[:8])
                return True
        logger.warning(
            "LMAF: no path to %s after %d path requests — the transport will drop this packet",
            source_hash[:8],
            LMAF_PATH_WAIT_ATTEMPTS,
        )
        return False

    async def _send_lmaf_transfer(self, packets, dest, source_hash, pacing):
        for position, content in enumerate(packets):
            if position:
                await asyncio.sleep(pacing)
            else:
                # Let the peer finish the burst it was transmitting when this
                # transfer was triggered (its report, plus the announces that
                # follow it): one radio cannot hear while it talks.
                await asyncio.sleep(LMAO_LMAF_TURNAROUND_SECONDS)
            await self._ensure_lmaf_path(dest, source_hash)
            self._send_lmaf_packet(dest, source_hash, content)

    def _send_lmaf_packet(self, dest, source_hash, content):
        """Send one LMAF envelope as a single opportunistic LXMF packet.

        Deliberately sent *without* an LXMF delivery receipt: reference LXMF
        waits for a Reticulum proof for every packet it hands to the router, and
        a peer that does not prove (the native clients) makes it retry 5x at 4 s
        intervals, drop and re-request the path, and finally discard the
        message ("Max delivery attempts reached").  That churn is both wasteful
        on a 1% duty-cycle medium and fatal to a multi-packet transfer, so the
        LMAF layer owns reliability instead: chunks that do not arrive are
        named in an AttachmentAck NEED and resent (see _handle_lmaf_ack), and a
        lost manifest is requested the same way.

        The legacy DATA-line reply keeps using the router's normal path.

        Runs inside the paced coroutine on the asyncio loop, so it must never
        let an exception escape (an unretrieved task exception would silently
        kill the rest of the transfer).
        """
        try:
            lxmf_msg = LXMF.LXMessage(
                destination=dest,
                source=_identity_to_destination(self.server_identity),
                content=content,
                title="p:Envelope",
                desired_method=LXMF.LXMessage.OPPORTUNISTIC,
            )
            lxmf_msg.pack()
            if lxmf_msg.method != LXMF.LXMessage.OPPORTUNISTIC:
                # Larger than one packet would allow: fall back to the router
                # (link delivery) rather than dropping the packet.
                self.router.handle_outbound(lxmf_msg)
                return True
            # Opportunistic framing: dest16 + src16 + signature + msgpack, and
            # the packet header already carries the destination hash.
            packet = RNS.Packet(dest, lxmf_msg.packed[16:])
            # send() returns False (and not None) when no interface could
            # process the packet, i.e. the transport dropped it.  Surface that:
            # an outbound LMAF packet discarded here would otherwise look like
            # a dead peer.
            if packet.send() is False:
                logger.warning(
                    "LMAF: transport dropped the packet for %s (no usable path)",
                    source_hash[:8],
                )
                return False
            # Diagnostic (issue #151): capture the exact on-air token (eph +
            # length) so it can be diffed against the device's received token
            # bytes to isolate framing corruption from a device-side decrypt
            # defect.  packet.raw is set by RNS.Packet.pack(); the token is the
            # frame minus flags|hops|dest|context.  Header width depends on the
            # header type (HEADER_2 carries a transport_id).
            try:
                raw = packet.raw if getattr(packet, "raw", None) else b""
                if raw:
                    hdr2 = (raw[0] & 0b01000000) >> 6
                    dst = 20 if hdr2 else 10
                    hdr_len = 2 + dst + 1
                    token = raw[hdr_len:]
                    eph = token[:32].hex()
                    logger.info(
                        "LMAF TX token dst=%s hdr=%s ratchet=%s token_len=%d eph=%s sha256=%s",
                        source_hash[:8],
                        "H2" if hdr2 else "H1",
                        (dest.latest_ratchet_id or b"").hex()[:12],
                        len(token),
                        eph,
                        hashlib.sha256(token).hexdigest(),
                    )
            except Exception:
                pass  # diagnostics must never break a send
            return True
        except Exception as e:
            logger.error("LMAF packet send failed for %s: %s", source_hash, e, exc_info=True)
            return False

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
            # destination must come from the payload itself — currently
            # only CommandRequest.target carries a destination identity.
            # Reticulum can only encrypt to a peer whose public keys were
            # previously learned (via an announce), so look the identity
            # up in the local cache with Identity.recall().
            dest_hash = ""
            if envelope.HasField("command"):
                dest_hash = envelope.command.target
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
    # SQLite receiver directory: the server learns devices as they report and
    # sends them downlinks even before/without a caps broadcast.  Persists in
    # the server's data directory next to the identity store.
    global CONTACTS
    contacts_db = os.environ.get("LMAO_CONTACTS_DB")
    if contacts_db is None:
        _id_dir = os.path.dirname(
            os.environ.get(
                "LMAO_SERVER_IDENTITY_PATH",
                os.path.expanduser("~/.local/share/lmao_server/lxmf"),
            )
        )
        contacts_db = os.path.join(_id_dir, "contacts.db")
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

    # Capture the running loop: the RNS delivery callback is sync and runs on
    # the RNS thread, so it schedules NATS publishes and paced LMAF sends
    # through this loop.
    lmao_server._loop = asyncio.get_event_loop()

    # Dead-letter retries ride the event loop: a transfer whose peer never
    # replied at all is re-offered (bounded) instead of silently dropped.
    lmaf_retry_task = asyncio.create_task(lmao_server._lmaf_retry_loop())

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
        lmaf_retry_task.cancel()
        if lmao_server:
            lmao_server.clear_grpc_subscribers()
            lmao_server._nats_queue = None  # Prevent new NATS publish attempts during shutdown
        if nats_queue is not None:
            await nats_queue.close()
            logger.info("NATS connection closed.")


if __name__ == "__main__":
    # When run directly, prefer the gRPC-enabled async main
    asyncio.run(async_main())
