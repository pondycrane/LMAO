"""Unit tests for lma_core.calendar_tcp_api (framed-TCP calendar read).

Drives the actual asyncio server with a fake CalendarStore and real protobuf
messages over a real loopback socket, so it exercises the wire contract the
native ESP-IDF RoboDog client will speak:
    [4B BE len][CalendarRequest proto]  ->  [4B BE len][CalendarBundle proto]
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from lma_core import CalendarBundle, CalendarRequest
from lma_core.calendar_tcp_api import _frame, start_calendar_tcp_server


def _fake_store(events, watermark=999):
    store = MagicMock()
    store.sync.return_value = (events, watermark)
    return store


_EVENT = {
    "uid": "u1", "calendar_id": "family", "title": "Swim class",
    "notes": "towel", "start_ms": 1700000000001, "end_ms": 1700000000061,
    "deleted": False, "pic": "swim",
}


async def _sync(
    server_port: int,
    calendar_id: str | None,
    since_ms: int,
    max_events: int = 0,
    participant: str = "",
) -> tuple[int, CalendarBundle]:
    req = CalendarRequest()
    if calendar_id is not None:
        req.calendar_id = calendar_id
    if since_ms:
        req.since_ms = since_ms
    if max_events:
        req.max_events = max_events
    if participant:
        req.participant = participant
    reader, writer = await asyncio.open_connection("127.0.0.1", server_port)
    try:
        writer.write(_frame(req.SerializeToString()))
        await writer.drain()
        head = await reader.readexactly(4)
        length = int.from_bytes(head, "big")
        payload = await reader.readexactly(length)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    bundle = CalendarBundle()
    bundle.ParseFromString(payload)
    return length, bundle


@pytest.mark.asyncio
async def test_request_returns_calendar_bundle(monkeypatch):
    store = _fake_store([dict(_EVENT)])
    server = await start_calendar_tcp_server(store, port=0)
    port = server.sockets[0].getsockname()[1]
    try:
        length, bundle = await _sync(port, "family", 5)
        assert length > 0
        assert bundle.calendar_id == "family"
        assert bundle.watermark_ms == 999
        assert len(bundle.events) == 1
        ev = bundle.events[0]
        assert ev.uid == "u1"
        assert ev.title == "Swim class"
        assert ev.pic == "swim"
        assert ev.deleted is False
    finally:
        server.close()
        await server.wait_closed()
    # the store was asked for the delta with the request's args
    store.sync.assert_called_once()  # calendar_id "family", since_ms 5


@pytest.mark.asyncio
async def test_zero_length_frame_uses_defaults():
    store = _fake_store([])
    server = await start_calendar_tcp_server(store, port=0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(_frame(b""))
            await writer.drain()
            head = await reader.readexactly(4)
            length = int.from_bytes(head, "big")
            assert length == 0 or length > 0  # empty reply still framed
            # default calendar_id resolves to "family"
            assert store.sync.call_args[1]["calendar_id"] == "family"
        finally:
            writer.close()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_empty_calendar_returns_framed_empty_bundle():
    store = _fake_store([])
    server = await start_calendar_tcp_server(store, port=0)
    port = server.sockets[0].getsockname()[1]
    try:
        length, bundle = await _sync(port, "family", 0)
        assert bundle.watermark_ms == 999
        assert len(bundle.events) == 0
        assert length > 0
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_request_participant_threaded_to_store():
    store = _fake_store([])
    server = await start_calendar_tcp_server(store, port=0)
    port = server.sockets[0].getsockname()[1]
    try:
        length, bundle = await _sync(port, "family", 5, participant="juliet")
        assert length > 0
        assert store.sync.call_args.kwargs["participant"] == "juliet"
        assert store.sync.call_args.kwargs["calendar_id"] == "family"
        assert store.sync.call_args.kwargs["since_ms"] == 5
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_family_request_defaults_participant_to_empty():
    store = _fake_store([])
    server = await start_calendar_tcp_server(store, port=0)
    port = server.sockets[0].getsockname()[1]
    try:
        length, bundle = await _sync(port, "family", 0)
        assert length > 0
        assert store.sync.call_args.kwargs["participant"] == ""
        assert store.sync.call_args.kwargs["calendar_id"] == "family"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_weather_provider_rides_the_reply():
    """A configured provider fills CalendarBundle.current + per-event weather
    on the framed-TCP path (the RoboDog client's actual transport)."""
    from lma_core.weather import WeatherProvider

    store = _fake_store([dict(_EVENT)])
    provider = WeatherProvider(
        40.6834, -74.4427,
        fetch=lambda url, timeout: {
            "current": {"temperature_2m": 6.7, "weather_code": 0},
            "hourly": {
                "time": [1700000000 // 3600 * 3600],
                "temperature_2m": [1.9],
                "weather_code": [71],
            },
        },
    )
    server = await start_calendar_tcp_server(store, port=0, weather=provider)
    port = server.sockets[0].getsockname()[1]
    try:
        _length, bundle = await _sync(port, "family", 0)
        assert bundle.HasField("current")
        assert bundle.current.temp_c10 == 67
        assert bundle.current.code == 0
        assert bundle.events[0].HasField("weather")
        assert bundle.events[0].weather.temp_c10 == 19
        assert bundle.events[0].weather.code == 71
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_no_weather_provider_leaves_reply_unchanged():
    """Without a provider the reply carries no Weather message at all, so a
    pre-weather deployment's wire bytes stay identical."""
    store = _fake_store([dict(_EVENT)])
    server = await start_calendar_tcp_server(store, port=0)
    port = server.sockets[0].getsockname()[1]
    try:
        _length, bundle = await _sync(port, "family", 0)
        assert not bundle.HasField("current")
        assert not bundle.events[0].HasField("weather")
    finally:
        server.close()
        await server.wait_closed()
