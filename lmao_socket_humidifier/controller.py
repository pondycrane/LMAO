"""LMAO socket humidifier — read the room, decide, drive the Atom Socket.

Per cycle (``--interval``, default 60 s):

1. ask the LMAO server for the RoboDog home node's air series
   (``HistoryRequest`` with the node pinned, so a second env reporter cannot
   hijack the reading);
2. apply the dew-point hysteresis + safety law
   (``lmao_socket_humidifier.control``);
3. renew or drop the Atom Socket's relay lease
   (``lmao_socket_humidifier.socket_client``);
4. persist the control state so a restart does not reset the daily budget or
   a cooldown (``--state-file``).

A failed read is NOT fatal: it is fed to the law as missing data, which turns
the humidifier OFF (fail-safe) — the socket's lease expires on its own if this
process dies entirely.

    bazel run //lmao_socket_humidifier:controller -- \
        --env-node "main bedroom" --socket humidifier.local --token "$HF_API_TOKEN"

Exit codes: 0 = ran (or --once succeeded), 1 = the socket never answered.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import json
import logging
import os
import signal
import sys
import tempfile
import threading
import time

from lmao_socket_humidifier.control import (
    ControlState,
    Decision,
    Setpoints,
    decide,
    dew_point_c,
    reconcile,
)
from lmao_socket_humidifier.socket_client import SocketClient, SocketError

logger = logging.getLogger("lmao_socket_humidifier")

DEFAULT_INTERVAL_S = 60
DEFAULT_STATE_FILE = os.path.expanduser("~/.local/state/lmao-humidifier/state.json")
# Warn at most this often about a relay that is closed but drawing standby
# power (unplugged appliance / empty tank) — a real fault, not a per-cycle event.
LOAD_WARN_INTERVAL_S = 900
STANDBY_WATTS = 2.0

# Set by SIGTERM/SIGINT: the loop exits and opens the relay before quitting, so
# a rollout or `systemctl stop` does not leave the humidifier running for the
# rest of the lease.
_STOP = threading.Event()


def _install_stop_handler() -> None:
    def handler(signum, _frame):
        logger.info("signal %d received — opening the relay before exit", signum)
        _STOP.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, handler)


def read_room(reader, env_node: str | None, now_s: float):
    """Return ``(temp_c, humidity_pct, sample_age_s)``; Nones on failure.

    ``sample_age_s`` is derived from the ChartBundle's ``start_ms`` (the
    server's receive stamp of the newest sample) when the clock is plausible;
    otherwise the caller passes None and the law treats the reading as stale.
    """
    summary = reader.request_env(count=1, node_id=env_node)
    if summary is None:
        logger.warning("no chart reply from the LMAO server")
        return None, None, None

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
        return None, None, None

    hum = summary.get("latest_humidity_pct")
    temp = summary.get("latest_temp_c")
    if hum is None or temp is None:
        # Dew point needs both; a temperature-only or humidity-only reply is
        # not a usable reading and the law fails safe on it.
        logger.warning(
            "server reply is missing %s (node=%s)",
            "humidity" if hum is None else "temperature",
            summary.get("node_id") or env_node,
        )
        return None, None, None

    start_ms = summary.get("start_ms") or 0
    now_ms = now_s * 1000.0
    age = (now_ms - start_ms) / 1000.0 if start_ms > 0 else None
    if age is not None and age < -300:
        # The board clock is behind the server's stamp: the age is meaningless,
        # so treat the sample as fresh-but-unknown rather than permanently stale.
        logger.warning("clock skew: sample stamp is %.0fs in the future", -age)
        age = 0.0
    return float(temp), int(hum), age


def apply_decision(client: SocketClient, decision: Decision, sp: Setpoints) -> dict:
    """Make the socket match the decision; return the socket's status reply."""
    if decision.relay_on:
        return client.set_on(sp.lease_s)
    return client.set_off()


def load_state(path: str) -> ControlState:
    """Read persisted control state; a missing/corrupt file starts fresh.

    A corrupt file is logged loudly rather than silently resetting: a reset
    also resets the daily budget, so it must be visible in the journal.
    """
    if not path:
        return ControlState()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        st = ControlState.from_dict(data)
        logger.info(
            "state restored from %s (relay_on=%s, on_accum=%.0fs, day=%d)",
            path,
            st.relay_on,
            st.on_accum_s,
            st.day_index,
        )
        return st
    except FileNotFoundError:
        logger.info("no state file at %s — starting fresh", path)
        return ControlState()
    except (OSError, ValueError, TypeError) as exc:
        logger.error("state file %s unreadable (%s) — starting fresh", path, exc)
        return ControlState()


def save_state(path: str, st: ControlState) -> None:
    """Atomically persist the control state (tmp + rename in the same dir)."""
    if not path:
        return
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(st.to_dict(), fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except OSError as exc:
        logger.error("could not write state file %s: %s", path, exc)


def load_warning(status: dict | None) -> str | None:
    """Return a warning when a closed relay is not driving a real load.

    The socket's HLW8032 only decodes while the relay is closed
    (``meter_fresh``), so a fresh reading near standby means the appliance is
    unplugged, switched off at its own switch, or the tank is empty.
    """
    if not status or not status.get("meter_fresh"):
        return None
    watts = status.get("power_w")
    if watts is None or watts >= STANDBY_WATTS:
        return None
    return (
        f"relay closed but the load draws {watts:.2f} W (< {STANDBY_WATTS} W) — "
        "appliance unplugged, switched off, or out of water"
    )


def _day_index(now_s: float) -> int:
    return _dt.datetime.fromtimestamp(now_s).date().toordinal()


def _parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Dew-point humidifier control via the LMAO server "
        "and an M5Stack Atom Socket"
    )
    p.add_argument(
        "--env-node",
        default=os.environ.get("LMAO_ENV_NODE"),
        help="Reporting node whose temperature/humidity is read "
        "(SensorReport.node_id, e.g. 'main bedroom')",
    )
    p.add_argument("--server-host", default=os.environ.get("LMAO_SERVER_HOST", "tp4"))
    p.add_argument(
        "--server-port", type=int, default=int(os.environ.get("LMAO_SERVER_PORT", "4246"))
    )
    p.add_argument(
        "--identity-dir",
        default=os.environ.get("HF_IDENTITY_DIR"),
        help="Reticulum identity directory (default: the home client's "
        "~/.local/share/lmao_home_client/lxmf)",
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
        "--state-file",
        default=os.environ.get("HF_STATE_FILE", DEFAULT_STATE_FILE),
        help="Persisted control state (daily budget, cooldown); '' disables "
        f"(default {DEFAULT_STATE_FILE})",
    )
    p.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("HF_INTERVAL", DEFAULT_INTERVAL_S)),
        help=f"poll cadence in seconds (default {DEFAULT_INTERVAL_S})",
    )
    p.add_argument("--low-dew", type=float, default=6.8, help="ON at/below this dew point (C)")
    p.add_argument("--high-dew", type=float, default=9.6, help="OFF at/above this dew point (C)")
    p.add_argument(
        "--rh-ceiling", type=int, default=60, help="hard OFF at/above this relative humidity %%"
    )
    p.add_argument(
        "--stale-s", type=int, default=900, help="treat the reading as stale after this long"
    )
    p.add_argument("--min-on-s", type=int, default=60)
    p.add_argument("--min-off-s", type=int, default=120)
    p.add_argument(
        "--max-on-s", type=int, default=3 * 3600, help="continuous-run cap before a forced cooldown"
    )
    p.add_argument("--cooldown-s", type=int, default=600)
    p.add_argument("--max-daily-on-s", type=int, default=8 * 3600)
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
        low_dew_c=args.low_dew,
        high_dew_c=args.high_dew,
        rh_ceiling_pct=args.rh_ceiling,
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
    state = load_state(args.state_file)

    # Reconcile the persisted state with the socket's actual relay before the
    # first decision.  The socket is authoritative: its lease expired while
    # this process was down, so a persisted relay_on=True is history, not
    # current fact — counting the downtime as ON time would spend the daily
    # budget on minutes the appliance never ran.
    if not args.dry_run:
        try:
            socket_status = client.status()
        except SocketError as exc:
            logger.warning("socket status unavailable at startup: %s", exc)
        else:
            actual = bool(socket_status.get("relay"))
            if reconcile(state, actual, time.time()):
                logger.info(
                    "state said relay=%s but the socket reports %s — adopting the socket",
                    not actual,
                    actual,
                )
            if actual:
                logger.info("relay is already closed; counting ON time from now")

    # Imported here so the module stays importable (and testable) without the
    # Reticulum client's ``import config`` path requirement.
    from robodog_home_client.client import HomeClient

    reader = HomeClient(
        server_host=args.server_host,
        server_port=args.server_port,
        identity_dir=args.identity_dir,
    )
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
    last_load_warn_s = 0.0
    graceful = False
    _STOP.clear()
    _install_stop_handler()
    try:
        while not _STOP.is_set():
            now_s = time.time()
            temp, hum, age = read_room(reader, args.env_node, now_s)
            dew = dew_point_c(temp, hum) if temp is not None and hum is not None else None
            decision = decide(sp, state, temp, hum, age, now_s, _day_index(now_s))
            save_state(args.state_file, state)

            if args.json:
                print(
                    json.dumps(
                        {
                            "temp_c": temp,
                            "humidity_pct": hum,
                            "dew_c": None if dew is None else round(dew, 2),
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
                    "T=%s RH=%s dew=%s age=%s -> relay %s (%s)",
                    f"{temp:.1f}C" if temp is not None else "none",
                    f"{hum}%" if hum is not None else "none",
                    f"{dew:.1f}C" if dew is not None else "none",
                    f"{age:.0f}s" if age is not None else "unknown",
                    "ON" if decision.relay_on else "OFF",
                    decision.reason,
                )

            if not args.dry_run:
                try:
                    status = apply_decision(client, decision, sp)
                except SocketError as exc:
                    logger.error("socket command failed: %s", exc)
                    exit_code = 1
                    if args.once:
                        return exit_code
                else:
                    if status.get("relay") is not decision.relay_on:
                        logger.error(
                            "socket reports relay=%s after commanding %s — command refused?",
                            status.get("relay"),
                            decision.relay_on,
                        )
                    warn = load_warning(status) if decision.relay_on else None
                    if warn and (now_s - last_load_warn_s) >= LOAD_WARN_INTERVAL_S:
                        logger.warning("%s", warn)
                        last_load_warn_s = now_s

            if args.once:
                return exit_code
            _STOP.wait(args.interval)
        graceful = True
    finally:
        if graceful and not args.dry_run:
            with contextlib.suppress(SocketError):
                client.set_off()
        reader.stop()
    logger.info("stopped; relay opened")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
