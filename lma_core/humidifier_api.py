"""Read side of the humidifier pull loop: the Atom Socket polls, nothing pushes.

**Why pull.** The socket's LAN address is a DHCP lease, so pushing ``POST /on``
to it couples actuation to the controller's ability to resolve that address.
On 2026-11-21 the bedroom socket's lease moved ``.22`` -> ``.21``; the
controller kept posting to the dead address for 8 h 23 m and the room dried to
34 % RH while the device itself was healthy the whole time (uptime 58 h,
``refusals=0``).  The socket's *outbound* path — the one its SensorReport
already uses every 60 s — has no such coupling: the socket knows where the
server is, the server never needs to know where the socket is.

**Who decides.** The controllers keep the dew-point law and its persisted
state (daily budget, cooldown, hysteresis) — one implementation, tunable from
a ConfigMap without a reflash.  They publish the decision here every cycle and
this module only relays it.

**The one rule.** A directive that has gone stale is never served.  A dead
controller therefore stops actuation at ``max_age_s`` instead of leaving the
relay closed on its last word; the socket's own lease remains the final
deadman.  The socket treats ``503`` as OFF immediately (not as a transport
error to ride out), so a controller outage opens the relay within one poll.

Endpoints
---------
POST /humidifier/{key}   publish {on, reason, lease_s, issued_s, fail_safe?}
                         Loopback-only: the controllers run hostNetwork on the
                         same node, so this is the host's own trust domain.
GET  /humidifier/{key}   the last fresh directive, or 503 when absent/stale
GET  /healthz            liveness + the keys currently held
"""

from __future__ import annotations

import threading
import time
from typing import Any

_logger = __import__("logging").getLogger(__name__)

try:
    import aiohttp.web
except Exception as exc:  # pragma: no cover - environment dependent
    aiohttp = None  # type: ignore[assignment]
    _AIOHTTP_IMPORT_ERROR = exc
else:
    _AIOHTTP_IMPORT_ERROR = None

DEFAULT_PORT = 8082
DEFAULT_MAX_AGE_S = 180
MAX_LEASE_S = 3600


def _require_aiohttp() -> None:
    if aiohttp is None:
        raise RuntimeError(
            "aiohttp is not installed. Humidifier directive API unavailable. "
            f"Install with: pip install aiohttp ({_AIOHTTP_IMPORT_ERROR})"
        )


def _is_loopback(peer: str | None) -> bool:
    if not peer:
        return False
    if peer.startswith("::ffff:"):
        peer = peer[len("::ffff:") :]
    return peer in ("127.0.0.1", "::1")


class DirectiveStore:
    """Last directive per socket key, with a staleness cutoff.

    In memory on purpose: a server restart empties the store, the socket gets
    ``503`` and opens the relay.  That is the safe direction (the controllers
    re-publish within one cycle), and it keeps the server stateless — the
    durable state lives in the controller's state file.
    """

    def __init__(self, max_age_s: int = DEFAULT_MAX_AGE_S, clock=None):
        self._max_age_s = int(max_age_s)
        self._clock = clock or time.time
        self._latest: dict[str, dict] = {}
        self._lock = threading.Lock()

    def publish(self, key: str, payload: Any) -> dict:
        """Validate and store one directive; raises ``ValueError`` on bad input."""
        if not isinstance(key, str) or not key or len(key) > 64:
            raise ValueError("key must be a non-empty string of at most 64 chars")
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")

        on = payload.get("on")
        if not isinstance(on, bool):
            raise ValueError("'on' must be a boolean")

        lease_s = payload.get("lease_s", 0)
        if not isinstance(lease_s, int) or isinstance(lease_s, bool):
            raise ValueError("'lease_s' must be an integer")
        if not (0 <= lease_s <= MAX_LEASE_S):
            raise ValueError(f"'lease_s' must be in [0, {MAX_LEASE_S}]")
        if on and lease_s == 0:
            raise ValueError("an ON directive must carry a positive lease")

        reason = payload.get("reason", "")
        if not isinstance(reason, str):
            raise ValueError("'reason' must be a string")

        issued_s = payload.get("issued_s")
        if issued_s is None:
            issued_s = self._clock()
        if not isinstance(issued_s, (int, float)) or isinstance(issued_s, bool):
            raise ValueError("'issued_s' must be a number")

        item = {
            "key": key,
            "on": on,
            "reason": reason[:200],
            "lease_s": lease_s,
            "issued_s": float(issued_s),
            "fail_safe": bool(payload.get("fail_safe", False)),
            "received_s": self._clock(),
        }
        with self._lock:
            self._latest[key] = item
        return dict(item)

    def get(self, key: str) -> dict | None:
        """The stored directive with ``age_s``, or None when absent/stale."""
        with self._lock:
            item = self._latest.get(key)
            if item is None:
                return None
            age_s = self._clock() - item["issued_s"]
            if age_s > self._max_age_s:
                return None
            out = dict(item)
        out["age_s"] = round(age_s, 1)
        return out

    def keys(self) -> list[str]:
        with self._lock:
            return sorted(self._latest)


def create_app(store: DirectiveStore) -> Any:  # aiohttp.web.Application
    """Create the aiohttp Application serving the directive store."""
    _require_aiohttp()
    routes = aiohttp.web.RouteTableDef()

    @routes.get("/healthz")
    async def health(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.json_response({"ok": True, "keys": store.keys()})

    @routes.get("/humidifier/{key}")
    async def read_directive(request: aiohttp.web.Request) -> aiohttp.web.Response:
        key = request.match_info["key"]
        item = store.get(key)
        if item is None:
            # 503 is the socket's OFF signal, not a transient error: it means
            # "no fresh decision", which must open the relay rather than let
            # the last lease ride.
            return aiohttp.web.json_response(
                {"error": "no fresh directive", "key": key}, status=503
            )
        return aiohttp.web.json_response(item)

    @routes.post("/humidifier/{key}")
    async def publish_directive(request: aiohttp.web.Request) -> aiohttp.web.Response:
        if not _is_loopback(request.remote):
            return aiohttp.web.json_response(
                {"error": "publish is loopback-only"}, status=403
            )
        try:
            body = await request.json()
        except Exception:
            return aiohttp.web.json_response({"error": "Invalid JSON body."}, status=400)
        try:
            item = store.publish(request.match_info["key"], body)
        except ValueError as exc:
            return aiohttp.web.json_response({"error": str(exc)}, status=400)
        return aiohttp.web.json_response(item, status=201)

    app = aiohttp.web.Application()
    app.add_routes(routes)
    return app


async def start_humidifier_server(
    store: DirectiveStore, host: str = "0.0.0.0", port: int = DEFAULT_PORT
) -> Any:  # aiohttp.web.AppRunner
    """Start the directive API. Returns an AppRunner to clean up."""
    _require_aiohttp()
    app = create_app(store)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, host=host, port=port)
    await site.start()
    _logger.info(
        "Humidifier directive API on %s:%d (socket polls GET /humidifier/<key>, "
        "controllers publish POST on loopback)",
        host,
        port,
    )
    return runner
