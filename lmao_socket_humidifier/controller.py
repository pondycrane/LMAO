"""LMAO socket humidifier — read the room, decide, drive the Atom Socket.

Per cycle (``--interval``, default 60 s):

1. ask the LMAO server for the RoboDog home node's air series
   (``HistoryRequest`` with the node pinned, so a second env reporter cannot
   hijack the reading);
2. apply the hysteresis + safety law (``lmao_socket_humidifier.control``);
3. renew or drop the Atom Socket's relay lease
   (``lmao_socket_humidifier.socket_client``).

A failed read is NOT fatal: it is fed to the law as missing data, which turns
the humidifier OFF (fail-safe) — the socket's lease expires on its own if this
process dies entirely.

    bazel run //lmao_socket_humidifier:controller -- \
        --env-node "main bedroom" --socket humidifier.local --token "$HF_API_TOKEN"

Exit codes: 0 = ran (or --once succeeded), 1 = the socket never answered.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import logging
import os
import sys
import time

from lmao_socket_humidifier.control import ControlState, Decision, Setpoints, decide
from lmao_socket_humidifier.socket_client import SocketClient, SocketError

logger = logging.getLogger("lmao_socket_humidifier")

DEFAULT_INTERVAL_S = 60


def read_humidity(reader, env_node: str | None, now_s: float):
    """Return ``(humidity_pct, sample_age_s)``; ``(None, None)`` on failure.

    ``sample_age_s`` is derived from the ChartBundle's ``start_ms`` (the
    server's receive stamp of the newest sample) when the clock is plausible;
    otherwise the caller passes None and the law treats the reading as stale.
    """
    summary = reader.request_env(count=1, node_id=env_node)
    if summary is None:
        logger.warning("no chart reply from the LMAO server")
        return None, None

    # The server falls back to the NEWEST reporting node when the requested
    # node_id is not a buffer key (e.g. a typo).  For an actuator that would
    # silently read another room's sensor, so a mismatch is treated as missing
    # data (fail-safe OFF) instead of being trusted.  ChartBundle.node_id is
    # the node's first 8 characters, so compare on that prefix.
    reply_node = summary.get("node_id") or ""
    if env_node and reply_node != env_node[:8]:
        logger.error(
            "server answered for node %r, not %r — refusing the reading "
            "(check --env-node against the board's SensorReport.node_id)",
            reply_node or "<none>",
            env_node,
        )
        return None, None

    hum = summary.get("latest_humidity_pct")
    if hum is None:
        logger.warning(
            "server reply has no humidity sample (node=%s)", summary.get("node_id") or env_node
        )
        return None, None

    start_ms = summary.get("start_ms") or 0
    now_ms = now_s * 1000.0
    age = (now_ms - start_ms) / 1000.0 if start_ms > 0 else None
    if age is not None and age < -300:
        # The board clock is behind the server's stamp: the age is meaningless,
        # so treat the sample as fresh-but-unknown rather than permanently stale.
        logger.warning("clock skew: sample stamp is %.0fs in the future", -age)
        age = 0.0
    return int(hum), age


def apply_decision(client: SocketClient, decision: Decision, sp: Setpoints) -> None:
    """Make the socket match the decision (lease-renew ON, or OFF)."""
    if decision.relay_on:
        client.set_on(sp.lease_s)
    else:
        client.set_off()


def _day_index(now_s: float) -> int:
    return _dt.datetime.fromtimestamp(now_s).date().toordinal()


def _parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Humidity-driven humidifier control via the LMAO server "
        "and an M5Stack Atom Socket"
    )
    p.add_argument(
        "--env-node",
        default=os.environ.get("LMAO_ENV_NODE"),
        help="Reporting node whose humidity is read (SensorReport.node_id, e.g. 'main bed')",
    )
    p.add_argument("--server-host", default=os.environ.get("LMAO_SERVER_HOST", "tp4"))
    p.add_argument(
        "--server-port", type=int, default=int(os.environ.get("LMAO_SERVER_PORT", "4246"))
    )
    p.add_argument(
        "--socket",
        default=os.environ.get("HF_SOCKET", "humidifier.local"),
        help="Atom Socket base URL or host, or a comma-separated fallback "
        "list (default humidifier.local)",
    )
    p.add_argument(
        "--token",
        default=os.environ.get("HF_API_TOKEN", ""),
        help="Socket API token (NVS-provisioned at flash time)",
    )
    p.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("HF_INTERVAL", DEFAULT_INTERVAL_S)),
        help=f"poll cadence in seconds (default {DEFAULT_INTERVAL_S})",
    )
    p.add_argument("--low", type=int, default=35, help="ON at/below this %%")
    p.add_argument("--high", type=int, default=40, help="OFF at/above this %%")
    p.add_argument(
        "--stale-s", type=int, default=900, help="treat the reading as stale after this long"
    )
    p.add_argument("--min-on-s", type=int, default=60)
    p.add_argument("--min-off-s", type=int, default=120)
    p.add_argument(
        "--max-on-s", type=int, default=2 * 3600, help="continuous-run cap before a forced cooldown"
    )
    p.add_argument("--cooldown-s", type=int, default=600)
    p.add_argument("--max-daily-on-s", type=int, default=5 * 3600)
    p.add_argument(
        "--lease-s", type=int, default=300, help="lease granted to the socket each ON cycle"
    )
    p.add_argument("--once", action="store_true", help="run exactly one cycle (bench verification)")
    p.add_argument(
        "--dry-run", action="store_true", help="decide and log, but do not command the socket"
    )
    p.add_argument("--json", action="store_true", help="one JSON line per cycle")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    sp = Setpoints(
        low_pct=args.low,
        high_pct=args.high,
        stale_s=args.stale_s,
        min_on_s=args.min_on_s,
        min_off_s=args.min_off_s,
        max_on_s=args.max_on_s,
        cooldown_s=args.cooldown_s,
        max_daily_on_s=args.max_daily_on_s,
        lease_s=args.lease_s,
    )
    sp.validate()

    client = SocketClient(args.socket, args.token)
    state = ControlState()

    # Imported here so the module stays importable (and testable) without the
    # Reticulum client's ``import config`` path requirement.
    from robodog_home_client.client import HomeClient

    reader = HomeClient(server_host=args.server_host, server_port=args.server_port)
    reader.start()
    if not reader.wait_for_interface():
        logger.error(
            "Reticulum TCP interface never came online — is %s:%d up?",
            args.server_host,
            args.server_port,
        )
        return 1
    if reader.resolve_server() == (None, None):
        logger.error("server announce never arrived; check the pinned dest hash or the mesh path")
        return 1

    exit_code = 0
    try:
        while True:
            now_s = time.time()
            hum, age = read_humidity(reader, args.env_node, now_s)
            decision = decide(sp, state, hum, age, now_s, _day_index(now_s))

            if args.json:
                import json

                print(
                    json.dumps(
                        {
                            "humidity_pct": hum,
                            "sample_age_s": None if age is None else round(age, 1),
                            "relay_on": decision.relay_on,
                            "reason": decision.reason,
                            "fail_safe": decision.fail_safe,
                            "on_total_s": round(state.on_total_s(now_s), 1),
                        }
                    ),
                    flush=True,
                )
            else:
                logger.info(
                    "humidity=%s age=%s -> relay %s (%s)",
                    f"{hum}%" if hum is not None else "none",
                    f"{age:.0f}s" if age is not None else "unknown",
                    "ON" if decision.relay_on else "OFF",
                    decision.reason,
                )

            if not args.dry_run:
                try:
                    apply_decision(client, decision, sp)
                except SocketError as exc:
                    logger.error("socket command failed: %s", exc)
                    exit_code = 1
                    if args.once:
                        return exit_code

            if args.once:
                return exit_code
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.info("stopping; relay is left to the socket lease")
        return exit_code
    finally:
        reader.stop()


if __name__ == "__main__":
    sys.exit(main())
