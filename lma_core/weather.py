"""Current conditions + hourly forecast for the family-calendar replies.

The RoboDog RLCD panel prints the weather beside the clock ("now 46°F clear")
and on the first event's row ("at 12:30 46°F snow showers").  The panel has no
weather sensor and no internet route of its own, so the LMAO server fetches
the forecast (Open-Meteo, no API key) and rides it on the SAME calendar reply
that already carries the events — one extra ~6-byte ``Weather`` submessage per
slot, no new client connection, no extra airtime.

Wire shape (``proto/lma_messages.proto``):

    Weather { sint32 temp_c10 = 1; uint32 code = 2; }
    CalendarBundle.current  = Weather   // conditions now
    CalEvent.weather        = Weather   // forecast at the event's start_ms

``temp_c10`` is tenths of a degree Celsius (integer, so the panel does the
°F/°C conversion with no float); ``code`` is the WMO 4677 weather code exactly
as Open-Meteo reports it, which every display maps to its own label/glyph.
Absent ``Weather`` means "no forecast for this slot" — clients MUST render
nothing rather than invent a value.

Fetching is deliberately cheap and failure-tolerant: one HTTPS GET per
``ttl_s`` (default 15 min, i.e. at most 4/hour for a 30-min-sync family panel),
the last good snapshot is kept across failures, and a network error yields no
weather rather than a broken calendar reply.  ``snapshot()`` is blocking
(``urllib``); async callers use ``asnapshot()`` which runs it in a worker
thread so the gRPC event loop is never held by DNS/TLS.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# 15 minutes: Open-Meteo updates its model output hourly, so a shorter TTL
# only adds requests, not accuracy.
DEFAULT_TTL_S = 900.0
DEFAULT_TIMEOUT_S = 8.0

# Open-Meteo's full hourly horizon (16 days = 384 samples, ~7 KB of JSON).
# A 3-day window looked sufficient but silently left the panel's FIRST event
# without a forecast whenever the next event was further out than the window
# (e.g. a kid panel whose only upcoming event is a Ninja Warrior class 4 days
# away): the slot then read as "no forecast" and the panel drew nothing.
# 16 days covers every event a family/kid panel renders; anything beyond it
# still correctly renders no forecast.  The payload size is a server-side
# fetch, not wire cost — the reply only carries the buckets its events use.
FORECAST_DAYS = 16

_HOUR_MS = 3_600_000

# One weather reading: {"temp_c10": int, "code": int}.
Wx = dict[str, int]


@dataclass(frozen=True)
class WeatherSnapshot:
    """Parsed Open-Meteo payload: conditions now + hourly forecast buckets."""

    current: Wx | None
    hours: dict[int, Wx] = field(default_factory=dict)

    def at(self, ts_ms: int) -> Wx | None:
        """Forecast bucket containing ``ts_ms`` (epoch ms, UTC), or None.

        Hourly samples are stamped at the START of their hour, so flooring the
        instant onto the hour grid picks the bucket the event starts inside —
        the reading a person would call "the weather when it starts".
        """
        if ts_ms <= 0:
            return None
        return self.hours.get((ts_ms // _HOUR_MS) * _HOUR_MS)


def _wx(temp_c: Any, code: Any) -> Wx | None:
    """Normalize one (temperature °C, WMO code) pair; None when unusable."""
    if temp_c is None or code is None:
        return None
    try:
        return {"temp_c10": int(round(float(temp_c) * 10)), "code": int(code)}
    except (TypeError, ValueError):
        return None


def parse_payload(payload: Any) -> WeatherSnapshot | None:
    """Build a snapshot from a decoded Open-Meteo JSON body.

    Hourly arrays are positional (``time[i]`` <-> ``temperature_2m[i]`` <->
    ``weather_code[i]``); a sample with a null temperature/code is skipped
    rather than zero-filled, because 0 °C / code 0 would read as a real
    "freezing and clear" forecast.
    """
    if not isinstance(payload, dict):
        return None
    current_raw = payload.get("current") or {}
    current = _wx(current_raw.get("temperature_2m"), current_raw.get("weather_code"))

    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    codes = hourly.get("weather_code") or []
    hours: dict[int, Wx] = {}
    for i, ts_s in enumerate(times):
        if i >= len(temps) or i >= len(codes):
            break
        wx = _wx(temps[i], codes[i])
        if wx is None or not isinstance(ts_s, (int, float)):
            continue
        hours[int(ts_s) * 1000] = wx

    if current is None and not hours:
        return None
    return WeatherSnapshot(current=current, hours=hours)


def build_url(latitude: float, longitude: float, url: str = OPEN_METEO_URL) -> str:
    """Open-Meteo request for current + hourly temp/code in UTC epoch seconds."""
    query = urllib.parse.urlencode(
        {
            "latitude": f"{latitude:.5f}",
            "longitude": f"{longitude:.5f}",
            "current": "temperature_2m,weather_code",
            "hourly": "temperature_2m,weather_code",
            "forecast_days": FORECAST_DAYS,
            # unixtime + UTC keeps the buckets on a plain epoch-hour grid, so
            # the event-start lookup needs no timezone arithmetic server-side
            # (the panel localizes the clock itself).
            "timeformat": "unixtime",
            "timezone": "UTC",
        }
    )
    return f"{url}?{query}"


class WeatherProvider:
    """TTL-cached Open-Meteo client.  Thread-safe enough for one server."""

    def __init__(
        self,
        latitude: float,
        longitude: float,
        ttl_s: float = DEFAULT_TTL_S,
        url: str = OPEN_METEO_URL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        fetch: Callable[[str, float], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.latitude = latitude
        self.longitude = longitude
        self.ttl_s = ttl_s
        self.url = url
        self.timeout_s = timeout_s
        self._fetch = fetch or _http_get_json
        self._clock = clock
        self._snapshot: WeatherSnapshot | None = None
        self._fetched_at: float | None = None

    # -- fetching -----------------------------------------------------------

    def snapshot(self) -> WeatherSnapshot | None:
        """Current snapshot: cached while fresh, refetched when stale.

        Never raises — a failed refresh falls back to the last good snapshot
        (or None when there never was one), so weather can only ever be
        missing from a reply, never break one.
        """
        now = self._clock()
        if (self._snapshot is not None and self._fetched_at is not None
                and now - self._fetched_at < self.ttl_s):
            return self._snapshot
        try:
            body = self._fetch(build_url(self.latitude, self.longitude, self.url),
                               self.timeout_s)
            snap = parse_payload(body)
        except Exception as exc:  # noqa: BLE001 — weather is best-effort
            _logger.warning("weather fetch failed (%s) — serving %s",
                            exc, "last snapshot" if self._snapshot else "nothing")
            return self._snapshot
        if snap is None:
            _logger.warning("weather payload unusable — serving %s",
                            "last snapshot" if self._snapshot else "nothing")
            return self._snapshot
        self._snapshot = snap
        self._fetched_at = now
        cur = snap.current
        _logger.info(
            "weather refreshed: now %s°C code=%s, %d hourly buckets",
            (cur["temp_c10"] / 10.0) if cur else "?", cur["code"] if cur else "?",
            len(snap.hours),
        )
        return snap

    async def asnapshot(self) -> WeatherSnapshot | None:
        """``snapshot()`` off the event loop (urllib blocks on DNS/TLS)."""
        return await asyncio.to_thread(self.snapshot)


def _http_get_json(url: str, timeout_s: float) -> Any:
    with urllib.request.urlopen(url, timeout=timeout_s) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def from_env(environ: dict[str, str] | None = None) -> WeatherProvider | None:
    """Provider from ``LMAO_WEATHER_LAT``/``LMAO_WEATHER_LON`` (None if unset).

    Weather is opt-in per deployment: a server without a household location
    sends no ``Weather`` at all, so existing clients see a byte-identical
    calendar reply.
    """
    env = os.environ if environ is None else environ
    lat_s = (env.get("LMAO_WEATHER_LAT") or "").strip()
    lon_s = (env.get("LMAO_WEATHER_LON") or "").strip()
    if not lat_s or not lon_s:
        return None
    try:
        lat = float(lat_s)
        lon = float(lon_s)
    except ValueError:
        _logger.warning("LMAO_WEATHER_LAT/LON not numeric (%r, %r) — weather off",
                        lat_s, lon_s)
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        _logger.warning("LMAO_WEATHER_LAT/LON out of range (%f, %f) — weather off",
                        lat, lon)
        return None
    try:
        ttl = float(env.get("LMAO_WEATHER_TTL_S") or DEFAULT_TTL_S)
    except ValueError:
        ttl = DEFAULT_TTL_S
    url = env.get("LMAO_WEATHER_URL") or OPEN_METEO_URL
    return WeatherProvider(lat, lon, ttl_s=ttl, url=url)


def apply_weather(bundle: Any, snapshot: WeatherSnapshot | None) -> int:
    """Fill ``bundle.current`` + each event's ``weather`` from a snapshot.

    Mutates the proto message in place (the same ``CalendarBundle`` shape is
    used by the gRPC, framed-TCP and LXMF calendar replies, so all three paths
    share this one attach rule).  Returns the number of slots filled, which is
    what the call sites log.  An event whose start is outside the forecast
    window simply keeps no weather.
    """
    if snapshot is None:
        return 0
    filled = 0
    if snapshot.current is not None:
        bundle.current.temp_c10 = snapshot.current["temp_c10"]
        bundle.current.code = snapshot.current["code"]
        filled += 1
    for entry in bundle.events:
        wx = snapshot.at(entry.start_ms)
        if wx is None:
            continue
        entry.weather.temp_c10 = wx["temp_c10"]
        entry.weather.code = wx["code"]
        filled += 1
    return filled
