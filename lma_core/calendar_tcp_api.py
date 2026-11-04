"""Framed-TCP family-calendar read service for the RoboDog display.

The native (ESP-IDF/Arduino) RoboDog deliberately skips RNS/LXMF and reads the
family calendar straight from the server over a tiny framed-TCP service.  It
reuses the exact LMAO wire messages (CalendarRequest / CalendarBundle), so the
on-device CalEvent shape is identical to the RNS path:

    client ──(TCP :LMAO_CAL_TCP_PORT)──▶ server
      [4-byte big-endian length][CalendarRequest protobuf bytes]
      (a zero-length frame = defaults: calendar_id "family", since_ms 0)
    server ──▶ client
      [4-byte big-endian length][CalendarBundle protobuf bytes]

Framing is plain length-prefixing (no HTTP/2, no gRPC), so the ESP-IDF side is
a lwIP socket + a protobuf decoder.  Read-only against the same CalendarStore
the RNS path uses; the trust model is the family LAN, like the contacts/query
APIs (no auth on purpose).

Bundle assembly is NOT duplicated here: ``lma_core.calendar_bundle.build_bundle``
is the single assembler shared with the gRPC and LXMF paths, so the full-fetch
tombstone rule, the single-headline ``pic_data`` rule and the weather attach
rule cannot drift between transports.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from lma_core import CalendarRequest
from lma_core.calendar_bundle import build_bundle

_logger = logging.getLogger(__name__)

# Upper bound for one request/reply frame (a family calendar delta is tiny;
# this only guards against a corrupt length prefix).
_MAX_FRAME = 1 << 20  # 1 MiB

_HEADER = 4


def _frame(payload: bytes) -> bytes:
    return len(payload).to_bytes(_HEADER, "big") + payload


async def _read_frame(reader: asyncio.StreamReader) -> bytes:
    head = await reader.readexactly(_HEADER)
    length = int.from_bytes(head, "big")
    if length > _MAX_FRAME:
        raise ValueError(f"calendar frame too large ({length} bytes)")
    if length == 0:
        return b""
    return await reader.readexactly(length)


async def _sync_to_bundle(
    store: Any,
    request: CalendarRequest,
    pic_store: Any = None,
    weather: Any = None,
) -> bytes:
    """Run a delta sync and serialize a CalendarBundle reply frame payload.

    ``weather`` is a ``lma_core.weather.WeatherProvider`` (or None when the
    deployment has no location configured); its blocking snapshot is fetched
    off the event loop, and the reply carries the same slots as gRPC/LXMF.
    """
    snapshot = await weather.asnapshot() if weather is not None else None
    bundle = build_bundle(store, request, pic_store=pic_store,
                          weather=snapshot)
    return bundle.SerializeToString()


async def _handle_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                       store: Any, pic_store: Any = None,
                       weather: Any = None) -> None:
    peer = writer.get_extra_info("peername")
    try:
        _logger.debug("calendar TCP client connected: %s", peer)
        data = await _read_frame(reader)
        req = CalendarRequest()
        if data:
            req.ParseFromString(data)
        payload = await _sync_to_bundle(store, req, pic_store, weather)
        writer.write(_frame(payload))
        await writer.drain()
        _logger.debug("calendar TCP reply (%d event-bytes) → %s", len(payload), peer)
    except asyncio.IncompleteReadError:
        _logger.debug("calendar TCP client disconnected mid-frame: %s", peer)
    except Exception:  # noqa: BLE001 - never crash the listener
        _logger.exception("calendar TCP request failed from %s", peer)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def start_calendar_tcp_server(
    store: Any,
    host: str = "0.0.0.0",
    port: int = 4247,
    pic_store: Any = None,
    weather: Any = None,
) -> asyncio.AbstractServer:
    """Start the framed-TCP calendar read service. Returns the `Server`.

    ``pic_store`` attaches the headline event's 1-bit RLCD tile (the same
    ``CalEvent.pic_data`` the gRPC path serves); ``weather`` (a
    ``lma_core.weather.WeatherProvider``) attaches the current conditions +
    per-event forecast to every reply.  Both None leaves the reply
    byte-identical to a pre-weather/pre-picture server.
    """
    server = await asyncio.start_server(
        lambda r, w: _handle_conn(r, w, store, pic_store, weather),
        host,
        port,
    )
    _logger.info("Calendar TCP read service listening on %s:%d", host, port)
    return server
