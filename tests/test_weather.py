"""Unit tests for lma_core.weather (Open-Meteo → CalendarBundle Weather slots).

Covers the pieces the RoboDog panel's weather actually depends on:
  - the epoch-hour bucketing used to find "the forecast when this event starts"
  - zigzag-negative temperatures surviving the wire (a winter panel must not
    print +4 °C for a -2 °C forecast)
  - TTL caching (at most one fetch per window) and never raising on a bad
    payload or a network error — weather can be missing, never fatal
  - opt-in configuration: no LMAO_WEATHER_LAT/LON → no Weather message at all,
    so a pre-weather deployment's reply bytes are unchanged
"""

import pytest

from lma_core import CalendarBundle
from lma_core.weather import (
    DEFAULT_TTL_S,
    WeatherProvider,
    apply_weather,
    build_url,
    from_env,
    parse_payload,
)

_HOUR_MS = 3_600_000

# 2026-11-01T05:00Z .. 07:00Z, hourly, with one null sample (a real Open-Meteo
# gap: null temperature, which must be skipped rather than zero-filled).
_PAYLOAD = {
    "current": {"time": 1793529000, "temperature_2m": 6.7, "weather_code": 0},
    "hourly": {
        "time": [1793523600, 1793527200, 1793530800, 1793534400],
        "temperature_2m": [5.1, None, 1.9, -2.5],
        "weather_code": [3, 61, 71, 71],
    },
}


def _provider(payload=_PAYLOAD, ttl=DEFAULT_TTL_S, clock=None):
    """Provider with a stub fetch (no network) and a controllable clock."""
    calls = []

    def fetch(url, timeout):
        calls.append(url)
        if isinstance(payload, Exception):
            raise payload
        return payload

    now = [0.0]
    p = WeatherProvider(
        40.6834, -74.4427, ttl_s=ttl, fetch=fetch,
        clock=clock or (lambda: now[0]),
    )
    return p, calls, now


def test_parse_payload_maps_current_and_hourly():
    snap = parse_payload(_PAYLOAD)
    assert snap is not None
    assert snap.current == {"temp_c10": 67, "code": 0}
    # Null-temperature samples are skipped, not zero-filled.
    assert len(snap.hours) == 3
    assert snap.hours[1793523600000] == {"temp_c10": 51, "code": 3}
    assert snap.hours[1793530800000] == {"temp_c10": 19, "code": 71}


def test_at_floors_to_the_hour_bucket():
    snap = parse_payload(_PAYLOAD)
    # An event starting 05:30Z falls in the 05:00Z bucket.
    assert snap.at(1793523600000 + 30 * 60 * 1000) == {"temp_c10": 51, "code": 3}
    # An event beyond the forecast horizon has no forecast.
    assert snap.at(1793534400000 + 24 * _HOUR_MS) is None
    # A missing start (0) never invents a reading.
    assert snap.at(0) is None


def test_negative_temperatures_round_trip_zigzag():
    """-2.5 °C is a negative sint32 on the wire; the panel must not read +4."""
    bundle = CalendarBundle()
    bundle.events.add().uid = "u1"
    bundle.events[0].start_ms = 1793534400000
    snap = parse_payload(_PAYLOAD)
    assert apply_weather(bundle, snap) == 2
    assert bundle.current.temp_c10 == 67
    assert bundle.events[0].weather.temp_c10 == -25
    assert bundle.events[0].weather.code == 71

    decoded = CalendarBundle()
    decoded.ParseFromString(bundle.SerializeToString())
    assert decoded.current.temp_c10 == 67
    assert decoded.events[0].weather.temp_c10 == -25
    assert decoded.events[0].HasField("weather")


def test_apply_weather_leaves_unforecast_events_empty():
    bundle = CalendarBundle()
    bundle.events.add().uid = "in-window"
    bundle.events[0].start_ms = 1793523600000
    bundle.events.add().uid = "far-future"
    bundle.events[1].start_ms = 1793534400000 + 48 * _HOUR_MS
    assert apply_weather(bundle, parse_payload(_PAYLOAD)) == 2  # current + 1 event
    assert bundle.events[0].HasField("weather")
    assert not bundle.events[1].HasField("weather")


def test_apply_weather_without_snapshot_changes_nothing():
    bundle = CalendarBundle()
    bundle.events.add().uid = "u1"
    assert apply_weather(bundle, None) == 0
    assert not bundle.HasField("current")
    assert not bundle.events[0].HasField("weather")


def test_snapshot_is_cached_for_the_ttl():
    p, calls, now = _provider(ttl=900.0)
    first = p.snapshot()
    assert first is not None and len(calls) == 1
    now[0] = 899.0
    assert p.snapshot() is first          # served from cache, no second fetch
    assert len(calls) == 1
    now[0] = 900.0
    p.snapshot()                          # TTL elapsed -> refetch
    assert len(calls) == 2


def test_fetch_failure_serves_last_good_snapshot():
    p, calls, now = _provider(ttl=0.0)
    good = p.snapshot()
    assert good is not None
    p._fetch = lambda url, timeout: (_ for _ in ()).throw(OSError("no route"))
    now[0] = 1.0
    assert p.snapshot() is good           # stale weather beats no weather
    assert len(calls) == 1


def test_fetch_failure_without_prior_snapshot_returns_none():
    p, _calls, _now = _provider(payload=OSError("dns fail"))
    assert p.snapshot() is None


def test_unusable_payload_is_not_cached():
    p, _calls, _now = _provider(payload={"unexpected": True})
    assert p.snapshot() is None
    assert p._snapshot is None


def test_build_url_requests_utc_unixtime_hourly():
    url = build_url(40.6834, -74.4427)
    assert url.startswith("https://api.open-meteo.com/v1/forecast?")
    assert "timeformat=unixtime" in url
    assert "timezone=UTC" in url
    assert "temperature_2m%2Cweather_code" in url


def test_from_env_disabled_without_coordinates(monkeypatch):
    monkeypatch.delenv("LMAO_WEATHER_LAT", raising=False)
    monkeypatch.delenv("LMAO_WEATHER_LON", raising=False)
    assert from_env({}) is None
    assert from_env({"LMAO_WEATHER_LAT": "40.68"}) is None


def test_from_env_enabled_with_coordinates():
    p = from_env({"LMAO_WEATHER_LAT": "40.6834", "LMAO_WEATHER_LON": "-74.4427"})
    assert p is not None
    assert p.latitude == pytest.approx(40.6834)
    assert p.longitude == pytest.approx(-74.4427)
    assert p.ttl_s == DEFAULT_TTL_S


def test_from_env_rejects_bad_values():
    assert from_env({"LMAO_WEATHER_LAT": "north", "LMAO_WEATHER_LON": "0"}) is None
    assert from_env({"LMAO_WEATHER_LAT": "91", "LMAO_WEATHER_LON": "0"}) is None
    assert from_env({"LMAO_WEATHER_LAT": "0", "LMAO_WEATHER_LON": "181"}) is None


def test_from_env_ttl_override():
    p = from_env({
        "LMAO_WEATHER_LAT": "40.68", "LMAO_WEATHER_LON": "-74.44",
        "LMAO_WEATHER_TTL_S": "60",
    })
    assert p is not None and p.ttl_s == 60.0


@pytest.mark.asyncio
async def test_asnapshot_runs_off_the_event_loop():
    p, calls, _now = _provider()
    snap = await p.asnapshot()
    assert snap is not None and len(calls) == 1
