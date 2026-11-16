"""HTTP API for the LMAO contact book (receiver directory + approval).

Runs beside the DuckDB query API as its own aiohttp app (separate port), so it
does not inherit the query gate's SQL-scope.  Lets a client answer the question
"who in the LMAO network do I want to send to" — by friendly name, device type,
or delivery hash — and lets the operator approve/revoke a device without
editing the server manifest and rolling a pod.

Endpoints
---------
GET  /contacts                    list every known device (?state=pending)
GET  /contacts/find?name=...      resolve a receiver by name
GET  /contacts/find?type=...      resolve by device type (cardputer, sprout, ...)
GET  /contacts/find?hash=...      resolve by delivery hash
POST /contacts                    register/upsert {name?, type, delivery_hash,
                                  pubkey_hex?, state?} — a POST is an operator
                                  act, so it defaults to state=approved
POST /contacts/{hash}/approve     approve a pending/revoked device
POST /contacts/{hash}/revoke      revoke a device
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)

try:
    import aiohttp.web
except Exception as exc:  # pragma: no cover - environment dependent
    aiohttp = None  # type: ignore[assignment]
    _AIOHTTP_IMPORT_ERROR = exc
else:
    _AIOHTTP_IMPORT_ERROR = None


def _require_aiohttp() -> None:
    if aiohttp is None:
        raise RuntimeError(
            "aiohttp is not installed. Contacts API unavailable. "
            f"Install with: pip install aiohttp ({_AIOHTTP_IMPORT_ERROR})"
        )


def _is_unique_violation(exc: Exception) -> bool:
    """True when *exc* is the ``device_name`` UNIQUE constraint (either backend).

    Narrow on purpose: a NOT NULL (or any other) integrity failure is a 500, not
    a misleading "name already in use" 409.
    """
    import sqlite3

    name = exc.__class__.__name__
    if not isinstance(exc, sqlite3.IntegrityError) and name not in (
        "UniqueViolation", "IntegrityError"
    ):
        return False
    return "device_name" in str(exc)


def create_app(book: Any) -> Any:  # aiohttp.web.Application
    """Create an aiohttp Application exposing the contact-book API.

    *book* is a contact book, or a zero-arg callable returning one.  The
    callable form lets the server start this API before Postgres is reachable
    (it answers 503 until the book reconnects) instead of leaving the operator
    with no approval path at all.
    """
    _require_aiohttp()
    routes = aiohttp.web.RouteTableDef()

    def _book() -> Any:
        return book() if callable(book) else book

    def _unavailable() -> Any:
        return aiohttp.web.json_response(
            {"error": "contact book unavailable — retry shortly"}, status=503
        )

    @routes.get("/contacts")
    async def list_contacts(request: aiohttp.web.Request) -> aiohttp.web.Response:
        current = _book()
        if current is None:
            return _unavailable()
        state = request.query.get("state")
        return aiohttp.web.json_response({"contacts": current.all(state=state)})

    @routes.get("/contacts/find")
    async def find_contact(request: aiohttp.web.Request) -> aiohttp.web.Response:
        current = _book()
        if current is None:
            return _unavailable()
        query = request.query
        matches = current.find(
            delivery_hash=query.get("hash"),
            device_name=query.get("name"),
            device_type=query.get("type"),
            state=query.get("state"),
        )
        return aiohttp.web.json_response({"contacts": matches})

    @routes.post("/contacts")
    async def register_contact(request: aiohttp.web.Request) -> aiohttp.web.Response:
        current = _book()
        if current is None:
            return _unavailable()
        try:
            body = await request.json()
        except Exception:
            return aiohttp.web.json_response({"error": "Invalid JSON body."}, status=400)
        if not isinstance(body, dict):
            return aiohttp.web.json_response({"error": "Expected a JSON object."}, status=400)
        delivery_hash = body.get("delivery_hash")
        device_type = body.get("type") or body.get("device_type")
        if not delivery_hash or not device_type:
            return aiohttp.web.json_response(
                {"error": "Both 'delivery_hash' and 'type' are required."}, status=400
            )
        # A POST is the operator's trust decision (install_all with the device
        # plugged in, or a manual curl), so it registers approved unless the
        # caller explicitly asks for another state.
        state = body.get("state") or "approved"
        try:
            entry = current.register(
                delivery_hash=delivery_hash,
                device_type=device_type,
                device_name=body.get("name"),
                pubkey_hex=body.get("pubkey_hex"),
                # A name given here is the operator's: it must survive the
                # device's own SensorReport.node_id / announce on every report.
                name_source="operator" if body.get("name") else "auto",
                state=state,
            )
        except ValueError as exc:
            return aiohttp.web.json_response({"error": str(exc)}, status=400)
        except Exception as exc:
            if _is_unique_violation(exc):
                return aiohttp.web.json_response(
                    {"error": f"device name {body.get('name')!r} is already in use"},
                    status=409,
                )
            raise
        return aiohttp.web.json_response({"contact": entry}, status=201)

    @routes.post("/contacts/{hash}/approve")
    async def approve_contact(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return _set_state(request, "approved")

    @routes.post("/contacts/{hash}/revoke")
    async def revoke_contact(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return _set_state(request, "revoked")

    def _set_state(request: aiohttp.web.Request,
                   state: str) -> aiohttp.web.Response:
        current = _book()
        if current is None:
            return _unavailable()
        delivery_hash = request.match_info["hash"].lower()
        if not current.set_state(delivery_hash, state):
            return aiohttp.web.json_response(
                {"error": f"unknown contact {delivery_hash}"}, status=404
            )
        return aiohttp.web.json_response(
            {"contact": current.find(delivery_hash=delivery_hash)[0]}
        )

    app = aiohttp.web.Application()
    app.add_routes(routes)
    return app


async def start_contacts_server(
    book: Any, host: str = "0.0.0.0", port: int = 8081
) -> Any:  # aiohttp.web.AppRunner
    """Start the contact-book HTTP API. Returns an AppRunner to clean up.

    *book* may be a zero-arg provider; see :func:`create_app`.
    """
    _require_aiohttp()
    app = create_app(book)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, host, port)
    await site.start()
    _logger.info("Contacts API listening on %s:%d", host, port)
    return runner
