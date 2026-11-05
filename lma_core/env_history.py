"""In-memory air temperature/humidity history for home clients.

The Sprout chart buffer (``sprout_history.SproutHistory``) deliberately only
tracks reports that carry a soil-moisture sample, because every other node on
the mesh — notably the Cardputer — reports its own temperature/humidity and
must not pollute the Sprout's soil chart.  That ownership rule is correct for
the chart, but it means a home client asking for ``AIR_TEMP`` / ``AIR_HUMIDITY``
gets no reply on a mesh without a Sprout.

This buffer is the complementary path: it keeps the latest air readings from
ANY node and answers an air-only ``HistoryRequest`` with a ``ChartBundle``
(empty soil/pump series).  The server chooses it only when the request selects
air series, so the chart semantics of ``SproutHistory`` are untouched.

Wire units match ``ChartBundle``:

* ``temp`` — tenths of a degree Celsius
* ``hum``  — integer percent

Readings are timestamped from ``SensorReading.timestamp_ms`` when present;
otherwise the server receive time is used.  The selected node is the one with
the newest sample, and ``node_id`` in the reply is that node's id (first 8
characters, matching the Sprout DATA-line convention).
"""

from __future__ import annotations

import time
from collections import deque

# Canonical sensor_id registry (proto/lma_messages.proto).
SENSOR_DIE_TEMP = 1
SENSOR_AIR_HUMIDITY = 2
SENSOR_AIR_TEMP = 3

DEFAULT_MAXLEN = 30
DEFAULT_PERIOD_MS = 5 * 60 * 1000


def _now_ms() -> int:
    return int(time.time() * 1000)


class _NodeSeries:
    """One node's air temperature/humidity rings (oldest first)."""

    __slots__ = ("temp", "hum", "last_seen_ms")

    def __init__(self, maxlen: int):
        self.temp = deque(maxlen=maxlen)  # (timestamp_ms, celsius)
        self.hum = deque(maxlen=maxlen)  # (timestamp_ms, percent)
        self.last_seen_ms = 0


class EnvHistory:
    """Recent air temperature/humidity samples, per reporting node."""

    def __init__(self, maxlen: int = DEFAULT_MAXLEN):
        self._maxlen = int(maxlen)
        self._nodes: dict[str, _NodeSeries] = {}

    def update(self, sensor_report) -> bool:
        """Fold one ``SensorReport`` into the buffer.

        Returns True when the report carried at least one air reading.
        A report without temperature or humidity is ignored, so unknown
        sensor types cannot disturb the buffer.
        """
        node = getattr(sensor_report, "node_id", "") or ""
        temp = None
        humidity = None
        timestamp_ms = 0

        for reading in getattr(sensor_report, "readings", None) or []:
            try:
                sensor_id = int(reading.sensor_id)
                value = float(reading.value)
            except (AttributeError, TypeError, ValueError):
                continue

            try:
                reading_ts = int(getattr(reading, "timestamp_ms", 0) or 0)
            except (TypeError, ValueError):
                reading_ts = 0
            timestamp_ms = max(timestamp_ms, reading_ts)

            if sensor_id == SENSOR_AIR_TEMP:
                temp = value
            elif sensor_id == SENSOR_AIR_HUMIDITY:
                humidity = value
            elif sensor_id == SENSOR_DIE_TEMP and temp is None:
                # Fallback for nodes that only expose the die temperature
                # (the Cardputer's default report).  An ambient reading (3)
                # always wins when present.
                temp = value

        if temp is None and humidity is None:
            return False

        if timestamp_ms <= 0:
            timestamp_ms = _now_ms()

        series = self._nodes.setdefault(node, _NodeSeries(self._maxlen))
        if temp is not None:
            series.temp.append((timestamp_ms, temp))
        if humidity is not None:
            series.hum.append((timestamp_ms, humidity))
        series.last_seen_ms = timestamp_ms
        return True

    def _latest_node(self) -> tuple[str, _NodeSeries] | None:
        if not self._nodes:
            return None
        return max(self._nodes.items(), key=lambda item: item[1].last_seen_ms)

    def latest(self) -> dict | None:
        """Return the newest node's latest air values as a plain dict."""
        selected = self._latest_node()
        if selected is None:
            return None
        node, series = selected
        temp = series.temp[-1] if series.temp else None
        humidity = series.hum[-1] if series.hum else None
        if temp is None and humidity is None:
            return None
        return {
            "node_id": node,
            "temp_c": temp[1] if temp else None,
            "humidity_pct": humidity[1] if humidity else None,
            "timestamp_ms": max(
                temp[0] if temp else 0,
                humidity[0] if humidity else 0,
            ),
        }

    @staticmethod
    def _period_ms(samples) -> int:
        """Median sample spacing, or the nominal 5-minute cadence."""
        times = [ts for ts, _value in samples]
        if len(times) < 2:
            return DEFAULT_PERIOD_MS
        deltas = sorted(b - a for a, b in zip(times, times[1:], strict=False))
        return deltas[len(deltas) // 2] or DEFAULT_PERIOD_MS

    def chart_bundle(
        self,
        count: int = 0,
        since_ms: int = 0,
        series=(),
        node_id: str | None = None,
    ):
        """Build a ``ChartBundle`` for one node's air history.

        ``node_id`` selects a specific reporting node (the requester's own
        device name, resolved by the server from its contact book / announce),
        so a multi-device mesh gets each device's own series instead of
        whichever node happened to report last.  ``None``/unknown falls back to
        the newest reporting node.

        ``since_ms`` drops samples older than that epoch-ms watermark.
        ``series`` is the request's selected ``Series`` set (empty = both
        air series).  Returns ``None`` when nothing matches.
        """
        selected = None
        if node_id and node_id in self._nodes:
            selected = (node_id, self._nodes[node_id])
        if selected is None:
            selected = self._latest_node()
        if selected is None:
            return None
        node, node_series = selected

        from proto.lma_messages_pb2 import ChartBundle as _ChartBundle
        from proto.lma_messages_pb2 import Series as _Series

        want_temp = (not series) or _Series.AIR_TEMP in series
        want_hum = (not series) or _Series.AIR_HUMIDITY in series

        temp_samples = (
            [(ts, value) for ts, value in node_series.temp if ts >= since_ms]
            if want_temp
            else []
        )
        hum_samples = (
            [(ts, value) for ts, value in node_series.hum if ts >= since_ms]
            if want_hum
            else []
        )
        if count:
            temp_samples = temp_samples[-count:]
            hum_samples = hum_samples[-count:]
        if not temp_samples and not hum_samples:
            return None

        all_times = [ts for ts, _ in temp_samples] + [ts for ts, _ in hum_samples]
        bundle = _ChartBundle()
        bundle.format = 1
        bundle.node_id = node[:8]
        bundle.period_ms = self._period_ms(
            temp_samples if temp_samples else hum_samples
        )
        bundle.start_ms = min(all_times)
        if temp_samples:
            bundle.temp.extend(int(round(value * 10)) for _, value in temp_samples)
        if hum_samples:
            bundle.hum.extend(int(round(value)) for _, value in hum_samples)
        return bundle

    def reset(self) -> None:
        self._nodes.clear()
