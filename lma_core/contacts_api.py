"""HTTP API for the LMAO contact book (receiver directory).

Runs beside the DuckDB query API as its own aiohttp app (separate port), so it
does not inherit the query gate's SQL-scope.  Lets a client answer the question
"who in the LMAO network do I want to send to" — by friendly name, device type,
or delivery hash — and register/nudge a device's directory entry.

Endpoints
---------
GET  /contacts                    list every known device
GET  /contacts/find?name=...      resolve a receiver by name
GET  /contacts/find?type=...      resolve by device type (cardputer, sprout, ...)
GET  /contacts/find?hash=...      resolve by delivery hash
POST /contacts                    register/upsert {name?, type, delivery_hash, pubkey_hex?}
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


def create_app(book: Any) -> Any:  # aiohttp.web.Application
    """Create an aiohttp Application exposing the contact-book API."""
    _require_aiohttp()
    routes = aiohttp.web.RouteTableDef()

    @routes.get("/contacts")
    async def list_contacts(_request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.json_response({"contacts": book.all()})

    @routes.get("/contacts/find")
    async def find_contact(request: aiohttp.web.Request) -> aiohttp.web.Response:
        query = request.query
        matches = book.find(
            delivery_hash=query.get("hash"),
            device_name=query.get("name"),
            device_type=query.get("type"),
        )
        return aiohttp.web.json_response({"contacts": matches})

    @routes.post("/contacts")
    async def register_contact(request: aiohttp.web.Request) -> aiohttp.web.Response:
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
        entry = book.register(
            delivery_hash=delivery_hash,
            device_type=device_type,
            device_name=body.get("name"),
            pubkey_hex=body.get("pubkey_hex"),
            identity_hash=body.get("identity_hash"),
        )
        return aiohttp.web.json_response({"contact": entry}, status=201)

    app = aiohttp.web.Application()
    app.add_routes(routes)
    return app


async def start_contacts_server(
    book: Any, host: str = "0.0.0.0", port: int = 8081
) -> Any:  # aiohttp.web.AppRunner
    """Start the contact-book HTTP API. Returns an AppRunner to clean up."""
    _require_aiohttp()
    app = create_app(book)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, host, port)
    await site.start()
    _logger.info("Contacts API listening on %s:%d", host, port)
    return runner
