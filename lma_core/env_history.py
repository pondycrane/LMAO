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

Samples are stored PAIRED — one ring entry per report, ``(ts, temp, hum)`` —
rather than two independent rings.  ``ChartBundle`` carries a flat list plus a
single ``period_ms``, so two rings of different length would be stretched over
the same x range independently and a report that carried only one reading would
shift the other trace in time.  Paired samples make the emitted temp/hum lists
the same length over the same time base whenever both are present.

Timestamps are the SERVER receive time, matching the ingest rule (issue #145):
a node's embedded ``SensorReading.timestamp_ms`` is not a reliable Unix-epoch
wall clock (unset, drifting, or scaled to a non-Unix epoch), and the chart's
x-axis labels are derived from it.  ``clock`` is injectable for tests.

``ChartBundle.temp`` is a ``uint32`` of tenths, so a sub-zero ambient reading
cannot be represented: such a sample is dropped (and logged) instead of
wrapping to ~4.29e9 tenths and blowing the chart's extent.

The selected node is the one the caller names (the requester's own
``SensorReport.node_id``); an unknown/absent name falls back to the node with
the newest sample.  ``node_id`` in the reply is that node's id (first 8
characters, matching the Sprout DATA-line convention).
"""

from __future__ import annotations

import logging
import time
from collections import deque

logger = logging.getLogger(__name__)

# Canonical sensor_id registry (proto/lma_messages.proto).
SENSOR_DIE_TEMP = 1
SENSOR_AIR_HUMIDITY = 2
SENSOR_AIR_TEMP = 3

DEFAULT_MAXLEN = 30
DEFAULT_PERIOD_MS = 5 * 60 * 1000


def _now_ms() -> int:
    return int(time.time() * 1000)


class _NodeSeries:
    """One node's paired air samples (oldest first)."""

    __slots__ = ("samples", "last_seen_ms")

    def __init__(self, maxlen: int):
        # (timestamp_ms, temp_c_or_None, humidity_or_None) — one entry per report.
        self.samples = deque(maxlen=maxlen)
        self.last_seen_ms = 0


class EnvHistory:
    """Recent air temperature/humidity samples, per reporting node."""

    def __init__(self, maxlen: int = DEFAULT_MAXLEN, clock=None):
        self._maxlen = int(maxlen)
        self._clock = clock or _now_ms
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

        for reading in getattr(sensor_report, "readings", None) or []:
            try:
                sensor_id = int(reading.sensor_id)
                value = float(reading.value)
            except (AttributeError, TypeError, ValueError):
                continue

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

        if temp is not None and temp < 0.0:
            logger.warning(
                "EnvHistory: dropping sub-zero temp %.1fC from %r — "
                "ChartBundle.temp is uint32 tenths and cannot carry it",
                temp, node,
            )
            temp = None
        if temp is None and humidity is None:
            return False

        timestamp_ms = int(self._clock())

        series = self._nodes.setdefault(node, _NodeSeries(self._maxlen))
        series.samples.append((timestamp_ms, temp, humidity))
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
        if not series.samples:
            return None
        timestamp_ms, temp, humidity = series.samples[-1]
        return {
            "node_id": node,
            "temp_c": temp,
            "humidity_pct": humidity,
            "timestamp_ms": timestamp_ms,
        }

    @staticmethod
    def _period_ms(samples) -> int:
        """Median sample spacing, or the nominal 5-minute cadence."""
        times = [ts for ts, _t, _h in samples]
        if len(times) < 2:
            return DEFAULT_PERIOD_MS
        deltas = sorted(b - a for a, b in zip(times, times[1:], strict=False))
        return deltas[len(deltas) // 2] or DEFAULT_PERIOD_MS

    @staticmethod
    def _select(samples, count, want_temp, want_hum):
        """Pick the sample window, preferring samples that carry both series.

        Both-series windows keep temp and hum the same length over the same
        time base.  When a report only ever carried one reading, fall back to
        that single series (the other list is then empty) rather than emitting
        two differently-sized lists that the client would stretch independently.
        """
        candidates = []
        if want_temp and want_hum:
            candidates.append([s for s in samples if s[1] is not None and s[2] is not None])
            candidates.append([s for s in samples if s[1] is not None])
            candidates.append([s for s in samples if s[2] is not None])
        elif want_temp:
            candidates.append([s for s in samples if s[1] is not None])
        else:
            candidates.append([s for s in samples if s[2] is not None])
        for selected in candidates:
            if selected:
                return selected[-count:] if count else selected
        return []

    def chart_bundle(
        self,
        count: int = 0,
        since_ms: int = 0,
        series=(),
        node_id: str | None = None,
    ):
        """Build a ``ChartBundle`` for one node's air history.

        ``node_id`` selects a specific reporting node (the requester's own
        ``SensorReport.node_id``, resolved by the server from what the device
        reports), so a multi-device mesh gets each device's own series instead
        of whichever node happened to report last.  ``None``/unknown falls back
        to the newest reporting node.

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

        window = [s for s in node_series.samples if s[0] >= since_ms]
        chosen = self._select(window, count, want_temp, want_hum)
        if not chosen:
            return None

        temp_samples = [(ts, t) for ts, t, _h in chosen if want_temp and t is not None]
        hum_samples = [(ts, h) for ts, _t, h in chosen if want_hum and h is not None]
        if not temp_samples and not hum_samples:
            return None

        all_times = [ts for ts, _ in temp_samples] + [ts for ts, _ in hum_samples]
        bundle = _ChartBundle()
        bundle.format = 1
        bundle.node_id = node[:8]
        bundle.period_ms = self._period_ms(chosen)
        bundle.start_ms = min(all_times)
        if temp_samples:
            bundle.temp.extend(int(round(value * 10)) for _, value in temp_samples)
        if hum_samples:
            bundle.hum.extend(int(round(value)) for _, value in hum_samples)
        return bundle

    def reset(self) -> None:
        self._nodes.clear()
