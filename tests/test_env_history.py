"""Host tests for ``lma_core/env_history.py`` (all-node air history)."""

import sys

from lma_core.env_history import (
    DEFAULT_MAXLEN,
    SENSOR_AIR_HUMIDITY,
    SENSOR_AIR_TEMP,
    SENSOR_DIE_TEMP,
    EnvHistory,
)


class Reading:
    def __init__(self, sensor_id, value, timestamp_ms=0):
        self.sensor_id = sensor_id
        self.value = value
        self.timestamp_ms = timestamp_ms


class Report:
    def __init__(self, node_id, readings):
        self.node_id = node_id
        self.readings = readings


def _report(node_id="cardputer01", temp=22.5, humidity=55.0, ts=1_700_000_000_000):
    readings = []
    if temp is not None:
        readings.append(Reading(SENSOR_AIR_TEMP, temp, ts))
    if humidity is not None:
        readings.append(Reading(SENSOR_AIR_HUMIDITY, humidity, ts))
    return Report(node_id, readings)


class TestEnvHistory:
    def test_empty_history_yields_no_latest_and_no_bundle(self):
        history = EnvHistory()
        assert history.latest() is None
        assert history.chart_bundle() is None

    def test_folds_air_readings_per_node(self):
        history = EnvHistory()
        assert history.update(_report(temp=21.0, humidity=50.0)) is True
        history.update(_report(temp=22.5, humidity=55.0, ts=1_700_000_300_000))
        latest = history.latest()
        assert latest["node_id"] == "cardputer01"
        assert latest["temp_c"] == 22.5
        assert latest["humidity_pct"] == 55.0
        assert latest["timestamp_ms"] == 1_700_000_300_000

    def test_report_without_air_readings_is_ignored(self):
        history = EnvHistory()
        assert history.update(Report("sprout", [Reading(4, 40.0, 1)])) is False
        assert history.latest() is None

    def test_die_temperature_is_the_fallback_when_no_ambient(self):
        history = EnvHistory()
        history.update(
            Report("cardputer01", [Reading(SENSOR_DIE_TEMP, 41.0, 1_700_000_000_000)])
        )
        assert history.latest()["temp_c"] == 41.0
        assert history.latest()["humidity_pct"] is None

    def test_ambient_temperature_wins_over_die_temperature(self):
        history = EnvHistory()
        history.update(
            Report(
                "cardputer01",
                [
                    Reading(SENSOR_DIE_TEMP, 41.0, 1_700_000_000_000),
                    Reading(SENSOR_AIR_TEMP, 21.5, 1_700_000_000_000),
                ],
            )
        )
        assert history.latest()["temp_c"] == 21.5

    def test_newest_node_is_selected(self):
        history = EnvHistory()
        history.update(_report(node_id="old", temp=20.0, ts=1_700_000_000_000))
        history.update(_report(node_id="new", temp=23.0, ts=1_700_000_100_000))
        assert history.latest()["node_id"] == "new"

    def test_chart_bundle_carries_tenths_and_percent(self):
        history = EnvHistory()
        history.update(_report(temp=21.4, humidity=50.0, ts=1_700_000_000_000))
        history.update(_report(temp=22.6, humidity=55.0, ts=1_700_000_300_000))
        bundle = history.chart_bundle()
        assert bundle.format == 1
        assert bundle.node_id == "cardpute", "node id truncated to 8 chars"
        assert list(bundle.temp) == [214, 226]
        assert list(bundle.hum) == [50, 55]
        assert bundle.start_ms == 1_700_000_000_000
        assert bundle.period_ms == 300_000
        assert len(bundle.soil) == 0 and bundle.watered == 0

    def test_single_sample_uses_the_nominal_period(self):
        history = EnvHistory()
        history.update(_report(temp=20.0, humidity=40.0, ts=1_700_000_000_000))
        bundle = history.chart_bundle()
        assert bundle.period_ms == 5 * 60 * 1000
        assert bundle.start_ms == 1_700_000_000_000

    def test_count_and_since_trim_the_window(self):
        history = EnvHistory()
        for i in range(5):
            history.update(
                _report(temp=20.0 + i, humidity=40.0 + i, ts=1_700_000_000_000 + i * 60_000)
            )
        bundle = history.chart_bundle(count=2)
        assert list(bundle.temp) == [230, 240]
        assert list(bundle.hum) == [43, 44]
        bundle = history.chart_bundle(since_ms=1_700_000_000_000 + 3 * 60_000)
        assert list(bundle.temp) == [230, 240]

    def test_series_selection_keeps_only_the_requested_one(self):
        history = EnvHistory()
        history.update(_report(temp=21.0, humidity=50.0, ts=1_700_000_000_000))
        from proto.lma_messages_pb2 import Series

        only_temp = history.chart_bundle(series={Series.AIR_TEMP})
        assert list(only_temp.temp) == [210]
        assert list(only_temp.hum) == []
        only_hum = history.chart_bundle(series={Series.AIR_HUMIDITY})
        assert list(only_hum.temp) == []
        assert list(only_hum.hum) == [50]

    def test_ring_keeps_only_the_newest_samples(self):
        history = EnvHistory(maxlen=3)
        for i in range(5):
            history.update(_report(temp=float(i), humidity=float(i), ts=1_700_000_000_000 + i))
        bundle = history.chart_bundle()
        assert list(bundle.temp) == [20, 30, 40]
        assert len(bundle.temp) == 3
        assert len(bundle.temp) <= DEFAULT_MAXLEN

    def test_node_id_selects_that_nodes_series(self):
        history = EnvHistory()
        history.update(_report(node_id="bedroom", temp=20.0, ts=1_700_000_000_000))
        history.update(_report(node_id="kitchen", temp=25.0, ts=1_700_000_100_000))
        # Newest node is "kitchen"; an explicit node wins over it.
        assert history.chart_bundle().node_id == "kitchen"
        bundle = history.chart_bundle(node_id="bedroom")
        assert bundle.node_id == "bedroom"
        assert list(bundle.temp) == [200]

    def test_unknown_node_falls_back_to_the_newest(self):
        history = EnvHistory()
        history.update(_report(node_id="bedroom", temp=20.0, ts=1_700_000_000_000))
        assert history.chart_bundle(node_id="nope").node_id == "bedroom"

    def test_reset_clears_the_history(self):
        history = EnvHistory()
        history.update(_report())
        history.reset()
        assert history.latest() is None
        assert history.chart_bundle() is None


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__] + sys.argv[1:]))
