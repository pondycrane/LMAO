"""RoboDog home client — air temperature & humidity from LMAO over Reticulum TCP.

A host-side (laptop / Pi) client for the LMAO mesh.  It joins the server's
Reticulum TCP interface (``LMAO_SERVER_HOST``:``LMAO_SERVER_PORT``, default
``tp4:4246`` — see ``config.py``), discovers the server's ``lxmf.delivery``
destination from its periodic announce, and asks for the air series:

    client -> server   LMAOEnvelope{ request.history }   (series AIR_TEMP +
                                                        AIR_HUMIDITY)
    server -> client   LMAOEnvelope{ chart }             (ChartBundle)

The server answers air-only history requests from its all-node air buffer
(``lma_core/env_history.py``), so any node's temperature/humidity reports —
Cardputer die/ambient temperature, Sprout SHT30 — can be read here.

Wire units: ``ChartBundle.temp`` is tenths of a degree Celsius, ``hum`` is
integer percent (see ``proto/lma_messages.proto``).

Usage:
    bazel run //robodog_home_client:home_client
    python3 robodog_home_client/client.py --json --count 5
    python3 robodog_home_client/client.py --interval 300

The client's ``lxmf/delivery`` hash must be in the server's ``ALLOWED_CLIENTS``
(``LMAO_ALLOWED_CLIENTS`` in ``k8s/lmao-server.yaml``); it is printed at start
and is stable across runs (identity persisted under ``--identity-dir``).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
import threading
import time

import config

from lma_core import LMAOEnvelope, Series
from lma_core.rns_di import LXMF, RNS
from lma_core.rns_init import init_rns_and_lxmf

logger = logging.getLogger(__name__)

DEFAULT_IDENTITY_DIR = os.path.expanduser("~/.local/share/lmao_home_client/lxmf")
DEFAULT_TIMEOUT_S = 30.0
SERVER_DISPLAY_NAME = "lmao-server"


# ───────────────────────────── wire helpers ─────────────────────────────


def build_history_request(seq=1, count=0, since_ms=0):
    """Serialize an air-only ``LMAOEnvelope{ request.history }``.

    ``count`` keeps the last N samples per series (0 = full ring);
    ``since_ms`` is a delta watermark (0 = everything).
    """
    envelope = LMAOEnvelope()
    envelope.seq = seq
    history = envelope.request.history
    history.count = int(count)
    history.since_ms = int(since_ms)
    history.series.extend([Series.AIR_TEMP, Series.AIR_HUMIDITY])
    return envelope.SerializeToString()


def parse_chart_reply(content):
    """Decode reply bytes into a ``ChartBundle``, or None.

    Accepts the raw LXMF content (a serialized ``LMAOEnvelope``).  Only a
    ``chart`` payload is returned; acks and text replies yield None.
    """
    try:
        envelope = LMAOEnvelope()
        envelope.ParseFromString(content)
    except Exception:
        return None
    if envelope.WhichOneof("payload") != "chart":
        return None
    return envelope.chart


def summarize_chart(chart):
    """Turn a ``ChartBundle`` into the client's plain result dict.

    ``temps_c`` are the wire values converted from tenths of °C.  ``latest``
    holds the newest sample of each series (None when that series is empty),
    so a caller can read one temperature and one humidity without walking the
    lists.
    """
    temps_c = [value / 10.0 for value in chart.temp]
    humidity = [int(value) for value in chart.hum]
    return {
        "node_id": chart.node_id,
        "period_ms": chart.period_ms,
        "start_ms": chart.start_ms,
        "temps_c": temps_c,
        "humidity_pct": humidity,
        "latest_temp_c": temps_c[-1] if temps_c else None,
        "latest_humidity_pct": humidity[-1] if humidity else None,
    }


def _c_to_f(celsius):
    return celsius * 9.0 / 5.0 + 32.0


def format_summary(summary):
    """Human-readable one-block rendering of a chart summary."""
    node = summary["node_id"] or "<unknown>"
    temp = summary["latest_temp_c"]
    hum = summary["latest_humidity_pct"]
    lines = [f"LMAO env — node {node}"]
    if temp is None:
        lines.append("  temperature: (none reported)")
    else:
        lines.append(f"  temperature: {temp:.1f} °C ({_c_to_f(temp):.1f} °F)")
    lines.append(
        "  humidity:    " + ("(none reported)" if hum is None else f"{hum} %")
    )
    if summary["temps_c"]:
        lines.append(
            "  temp series: "
            + ", ".join(f"{value:.1f}" for value in summary["temps_c"])
            + " °C"
        )
    if summary["humidity_pct"]:
        lines.append(
            "  hum series:  "
            + ", ".join(str(value) for value in summary["humidity_pct"])
            + " %"
        )
    return "\n".join(lines)


# ─────────────────────────────── discovery ──────────────────────────────


def _announce_display_name(app_data):
    """Extract the LXMF announce display name (msgpack peer data), or None."""
    if not app_data:
        return None
    try:
        import RNS.vendor.umsgpack as umsgpack

        peer_data = umsgpack.unpackb(app_data)
        name = peer_data[0]
        if isinstance(name, bytes):
            name = name.decode("utf-8", "replace")
        return name
    except Exception:
        return None


class _ServerAnnounceHandler:
    """Collect the server's ``lxmf.delivery`` destination from its announce.

    Matches either an explicit ``--dest-hash`` pin or the server's LXMF display
    name (``lmao-server``), so another mesh node's announce is not mistaken for
    the server.
    """

    aspect_filter = "lxmf.delivery"
    receive_path_responses = True

    def __init__(self, dest_hash_hex=None, own_hash_hex=None):
        self.dest_hash_hex = (dest_hash_hex or "").lower() or None
        self.own_hash_hex = (own_hash_hex or "").lower() or None
        self.destination_hash = None
        self.identity = None
        self.event = threading.Event()

    def received_announce(
        self,
        destination_hash,
        announced_identity,
        app_data,
        announce_packet_hash=None,
        is_path_response=False,
    ):
        hash_hex = RNS.hexrep(destination_hash, delimit=False).lower()
        if self.own_hash_hex and hash_hex == self.own_hash_hex:
            return  # our own delivery destination
        if self.dest_hash_hex and hash_hex != self.dest_hash_hex:
            return
        if not self.dest_hash_hex:
            name = _announce_display_name(app_data)
            if name != SERVER_DISPLAY_NAME:
                return
        self.destination_hash = bytes(destination_hash)
        self.identity = announced_identity
        self.event.set()


# ──────────────────────────────── client ────────────────────────────────


class HomeClient:
    """Reticulum TCP lifecycle + air-series request/reply handling."""

    def __init__(
        self,
        server_host=None,
        server_port=None,
        dest_hash=None,
        identity_dir=None,
        timeout=DEFAULT_TIMEOUT_S,
        config_dict=None,
        configdir_factory=None,
    ):
        self.router = None
        self.identity = None
        self._server_host = server_host or os.environ.get("LMAO_SERVER_HOST", "tp4")
        self._server_port = int(server_port or os.environ.get("LMAO_SERVER_PORT", "4246"))
        self._dest_hash = (dest_hash or os.environ.get("LMAO_DEST_HASH") or "").lower() or None
        self._identity_dir = identity_dir or DEFAULT_IDENTITY_DIR
        self._timeout = float(timeout)
        self._config_dict = config_dict
        self._configdir_factory = configdir_factory
        self._announce = None
        self._chart = None
        self._chart_event = threading.Event()
        self._seq = 0

    def _make_config(self):
        return config.make_config(self._server_host, self._server_port)

    def _target(self):
        if self._config_dict is not None:
            cfg = self._config_dict
        else:
            cfg = self._make_config().get_config_dict()
        iface = cfg.get("interfaces", {}).get("Home network TCP", {})
        return (
            iface.get("target_host", self._server_host),
            iface.get("target_port", self._server_port),
        )

    # ---- lifecycle -----------------------------------------------------

    def start(self):
        """Initialise Reticulum/LXMF, announce, and resolve the server."""
        factory = self._configdir_factory or self._make_config().get_configdir
        self.identity, self.router = init_rns_and_lxmf(
            rnode_port="",
            configdir_factory=factory,
            identity_storage_path=self._identity_dir,
            display_name="robodog-home",
            register_delivery_callback=lambda r: r.register_delivery_callback(
                self.handle_lxmf_delivery
            ),
            rnode_exists=False,
        )
        own_hash = self._delivery_hash_hex()
        logger.info("Client lxmf/delivery hash: %s", own_hash)

        for dest_hash in list(self.router.delivery_destinations):
            self.router.announce(dest_hash)

        self._announce = _ServerAnnounceHandler(
            dest_hash_hex=self._dest_hash, own_hash_hex=own_hash
        )
        RNS.Transport.register_announce_handler(self._announce)

        # A pinned hash may be unknown to this fresh transport instance; ask
        # for its path explicitly so the announce/path response can resolve it.
        if self._dest_hash:
            try:
                RNS.Transport.request_path(bytes.fromhex(self._dest_hash))
            except (ValueError, OSError) as exc:
                logger.warning("Path request for %s failed: %s", self._dest_hash, exc)

        return own_hash

    def stop(self):
        if self._announce is not None:
            with contextlib.suppress(Exception):
                RNS.Transport.deregister_announce_handler(self._announce)
            self._announce = None

    def _delivery_hash_hex(self):
        dest = RNS.Destination(
            self.identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            "lxmf",
            "delivery",
        )
        return RNS.hexrep(dest.hash, delimit=False)

    def resolve_server(self, timeout=None):
        """Block until the server's delivery identity is known.

        Returns ``(destination_hash_bytes, identity)`` or ``(None, None)``.
        """
        timeout = self._timeout if timeout is None else float(timeout)
        if self._announce is None:
            return None, None
        if not self._announce.event.wait(timeout):
            return None, None
        return self._announce.destination_hash, self._announce.identity

    def wait_for_interface(self, timeout=None):
        """Block until the TCP interface is actually connected.

        Reticulum's ``TCPClientInterface`` connects in its own thread; a packet
        handed to ``Transport`` before that thread reports ``online`` is
        silently dropped (no error, no retry), so a request sent the instant
        the server's announce lands can vanish.  Returns True when connected.
        """
        timeout = self._timeout if timeout is None else float(timeout)
        deadline = time.time() + timeout
        while time.time() < deadline:
            for iface in RNS.Transport.interfaces:
                if getattr(iface, "online", False):
                    return True
            time.sleep(0.2)
        logger.warning("No online Reticulum interface after %.0fs", timeout)
        return False

    # ---- receive -------------------------------------------------------

    def handle_lxmf_delivery(self, message):
        """Record the first ChartBundle reply; ignore everything else."""
        content = getattr(message, "content", b"") or b""
        chart = parse_chart_reply(content)
        if chart is None:
            return
        self._chart = chart
        self._chart_event.set()

    # ---- request -------------------------------------------------------

    def request_env(self, count=0, since_ms=0, timeout=None):
        """Send one air-series request and wait for the ChartBundle.

        Returns the chart summary dict, or None when the server does not
        answer (unknown/unallow-listed client, no samples, or timeout).
        """
        timeout = self._timeout if timeout is None else float(timeout)
        dest_hash, dest_identity = self.resolve_server(timeout)
        if dest_identity is None:
            host, port = self._target()
            logger.error(
                "Server lxmf.delivery destination not found within %.0fs — "
                "is %s:%s reachable and announcing?",
                timeout,
                host,
                port,
            )
            return None
        if not self.wait_for_interface(timeout):
            host, port = self._target()
            logger.error("Reticulum TCP interface to %s:%s never came online",
                         host, port)
            return None

        self._chart = None
        self._chart_event.clear()
        self._seq += 1

        destination = RNS.Destination(
            dest_identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            "lxmf",
            "delivery",
        )
        outbound = LXMF.LXMessage(
            destination=destination,
            source=RNS.Destination(
                self.identity,
                RNS.Destination.OUT,
                RNS.Destination.SINGLE,
                "lxmf",
                "delivery",
            ),
            content=build_history_request(
                seq=self._seq, count=count, since_ms=since_ms
            ),
            title="p:Envelope",
            desired_method=LXMF.LXMessage.OPPORTUNISTIC,
        )
        self.router.handle_outbound(outbound)
        logger.info(
            "Air-series request sent to %s (seq=%d, count=%d, since_ms=%d)",
            RNS.hexrep(dest_hash, delimit=False),
            self._seq,
            count,
            since_ms,
        )

        if not self._chart_event.wait(timeout):
            logger.warning("No ChartBundle reply within %.0fs", timeout)
            return None
        return summarize_chart(self._chart)


# ──────────────────────────────── main ──────────────────────────────────


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Fetch air temperature & humidity from the LMAO server over the "
            "Reticulum TCP interface."
        )
    )
    parser.add_argument(
        "--server-host",
        default=os.environ.get("LMAO_SERVER_HOST", "tp4"),
        help="LMAO server LAN hostname (default: $LMAO_SERVER_HOST or tp4)",
    )
    parser.add_argument(
        "--server-port",
        type=int,
        default=int(os.environ.get("LMAO_SERVER_PORT", "4246")),
        help="LMAO server RNS TCP port (default: $LMAO_SERVER_PORT or 4246)",
    )
    parser.add_argument(
        "--dest-hash",
        default=os.environ.get("LMAO_DEST_HASH"),
        help="Pin the server's lxmf/delivery hash (hex) instead of matching "
        "its announce display name",
    )
    parser.add_argument(
        "--identity-dir",
        default=DEFAULT_IDENTITY_DIR,
        help=f"Client identity directory (default: {DEFAULT_IDENTITY_DIR})",
    )
    parser.add_argument(
        "--count", type=int, default=0, help="Samples per series (0 = full ring)"
    )
    parser.add_argument(
        "--since-ms", type=int, default=0, help="Delta watermark (0 = full)"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=f"Seconds to wait for the announce / reply (default: {DEFAULT_TIMEOUT_S})",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0,
        help="Poll every N seconds (0 = fetch once and exit)",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON")
    parser.add_argument(
        "--verbose", action="store_true", help="Enable INFO logging"
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    client = HomeClient(
        server_host=args.server_host,
        server_port=args.server_port,
        dest_hash=args.dest_hash,
        identity_dir=args.identity_dir,
        timeout=args.timeout,
    )
    own_hash = client.start()
    print(f"Client lxmf/delivery hash: {own_hash} (must be in ALLOWED_CLIENTS)")

    exit_code = 1
    try:
        while True:
            summary = client.request_env(count=args.count, since_ms=args.since_ms)
            if summary is None:
                exit_code = 1
            else:
                exit_code = 0
                if args.json:
                    print(json.dumps(summary, sort_keys=True))
                else:
                    print(format_summary(summary))
            if args.interval <= 0:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        exit_code = 0
    finally:
        client.stop()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
