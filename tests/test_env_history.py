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


class Clock:
    """Injectable server-receive clock: each call returns the next stamp."""

    def __init__(self, *stamps):
        self._stamps = list(stamps)
        self._i = 0

    def __call__(self):
        if self._i < len(self._stamps):
            value = self._stamps[self._i]
            self._i += 1
            return value
        return self._stamps[-1] + 60_000


class TestEnvHistory:
    def test_empty_history_yields_no_latest_and_no_bundle(self):
        history = EnvHistory()
        assert history.latest() is None
        assert history.chart_bundle() is None

    def test_folds_air_readings_per_node(self):
        history = EnvHistory(clock=Clock(1_700_000_000_000, 1_700_000_300_000))
        history.update(_report(temp=22.5, humidity=55.0))
        history.update(_report(temp=23.0, humidity=56.0))
        latest = history.latest()
        assert latest["node_id"] == "cardputer01"
        assert latest["temp_c"] == 23.0
        assert latest["humidity_pct"] == 56.0
        assert latest["timestamp_ms"] == 1_700_000_300_000

    def test_readings_are_stamped_with_server_receive_time(self):
        """The node's own timestamp_ms is not a trustworthy wall clock
        (issue #145): the chart's x-axis labels come from these stamps."""
        history = EnvHistory(clock=Clock(1_700_000_500_000))
        history.update(_report(ts=12345))  # bogus device clock
        bundle = history.chart_bundle()
        assert bundle.start_ms == 1_700_000_500_000

    def test_report_without_air_readings_is_ignored(self):
        history = EnvHistory()
        assert history.update(Report("sprout", [Reading(4, 40.0, 1)])) is False
        assert history.latest() is None

    def test_die_temperature_is_the_fallback_when_no_ambient(self):
        history = EnvHistory(clock=Clock(1_700_000_000_000))
        history.update(Report("cardputer01", [Reading(SENSOR_DIE_TEMP, 41.0)]))
        assert history.latest()["temp_c"] == 41.0
        assert history.latest()["humidity_pct"] is None

    def test_ambient_temperature_wins_over_die_temperature(self):
        history = EnvHistory(clock=Clock(1_700_000_000_000))
        history.update(Report("node", [
            Reading(SENSOR_DIE_TEMP, 41.0),
            Reading(SENSOR_AIR_TEMP, 21.5),
        ]))
        assert history.latest()["temp_c"] == 21.5

    def test_sub_zero_temperature_is_dropped_not_wrapped(self):
        """ChartBundle.temp is uint32 tenths: -5 C would wrap to ~4.29e9."""
        history = EnvHistory(clock=Clock(1_700_000_000_000))
        assert history.update(_report(temp=-5.0, humidity=80.0)) is True
        latest = history.latest()
        assert latest["temp_c"] is None
        assert latest["humidity_pct"] == 80.0
        bundle = history.chart_bundle()
        assert len(bundle.temp) == 0
        assert list(bundle.hum) == [80]

    def test_report_with_only_a_sub_zero_temp_is_ignored(self):
        history = EnvHistory(clock=Clock(1_700_000_000_000))
        assert history.update(_report(temp=-2.0, humidity=None)) is False
        assert history.latest() is None

    def test_newest_node_is_selected(self):
        history = EnvHistory(clock=Clock(1_700_000_000_000, 1_700_000_600_000))
        history.update(_report(node_id="old", temp=20.0))
        history.update(_report(node_id="new", temp=25.0))
        assert history.latest()["node_id"] == "new"

    def test_chart_bundle_carries_tenths_and_percent(self):
        history = EnvHistory(clock=Clock(1_700_000_000_000, 1_700_000_300_000))
        history.update(_report(temp=21.5, humidity=50.0))
        history.update(_report(temp=22.6, humidity=55.0))
        bundle = history.chart_bundle()
        assert list(bundle.temp) == [215, 226]
        assert list(bundle.hum) == [50, 55]
        assert bundle.format == 1
        assert bundle.period_ms == 300_000
        assert bundle.start_ms == 1_700_000_000_000
        assert len(bundle.soil) == 0 and bundle.watered == 0

    def test_single_sample_uses_the_nominal_period(self):
        history = EnvHistory(clock=Clock(1_700_000_000_000))
        history.update(_report())
        bundle = history.chart_bundle()
        assert bundle.period_ms == 5 * 60 * 1000
        assert bundle.start_ms == 1_700_000_000_000

    def test_count_and_since_trim_the_window(self):
        history = EnvHistory(
            clock=Clock(1_700_000_000_000, 1_700_000_300_000, 1_700_000_600_000)
        )
        history.update(_report(temp=21.0, humidity=50.0))
        history.update(_report(temp=22.0, humidity=51.0))
        history.update(_report(temp=23.0, humidity=52.0))
        assert list(history.chart_bundle(count=2).temp) == [220, 230]
        trimmed = history.chart_bundle(since_ms=1_700_000_300_000)
        assert list(trimmed.temp) == [220, 230]
        assert trimmed.start_ms == 1_700_000_300_000

    def test_series_selection_keeps_only_the_requested_one(self):
        from proto.lma_messages_pb2 import Series

        history = EnvHistory(clock=Clock(1_700_000_000_000))
        history.update(_report(temp=22.0, humidity=50.0))
        only_temp = history.chart_bundle(series=[Series.AIR_TEMP])
        assert list(only_temp.temp) == [220] and list(only_temp.hum) == []
        only_hum = history.chart_bundle(series=[Series.AIR_HUMIDITY])
        assert list(only_hum.temp) == [] and list(only_hum.hum) == [50]

    def test_series_stay_time_aligned_when_a_report_lacks_one_reading(self):
        """ChartBundle has ONE period_ms: two lists of different length would be
        stretched over the same x range independently and drift apart."""
        history = EnvHistory(
            clock=Clock(1_700_000_000_000, 1_700_000_300_000, 1_700_000_600_000)
        )
        history.update(_report(temp=21.0, humidity=50.0))
        history.update(_report(temp=22.0, humidity=None))   # humidity missing
        history.update(_report(temp=23.0, humidity=52.0))
        bundle = history.chart_bundle()
        assert len(bundle.temp) == len(bundle.hum)
        assert list(bundle.temp) == [210, 230]
        assert list(bundle.hum) == [50, 52]
        assert bundle.start_ms == 1_700_000_000_000
        assert bundle.period_ms == 600_000  # the two kept samples' spacing

    def test_single_series_mesh_falls_back_to_that_series(self):
        history = EnvHistory(clock=Clock(1_700_000_000_000, 1_700_000_300_000))
        history.update(_report(temp=21.0, humidity=None))
        history.update(_report(temp=22.0, humidity=None))
        bundle = history.chart_bundle()
        assert list(bundle.temp) == [210, 220]
        assert list(bundle.hum) == []

    def test_ring_keeps_only_the_newest_samples(self):
        history = EnvHistory(maxlen=3, clock=Clock(*[1_700_000_000_000 + i * 300_000
                                                    for i in range(5)]))
        for i in range(5):
            history.update(_report(temp=20.0 + i))
        bundle = history.chart_bundle()
        assert list(bundle.temp) == [220, 230, 240]
        assert len(history.chart_bundle(count=0).temp) <= DEFAULT_MAXLEN

    def test_node_id_selects_that_nodes_series(self):
        history = EnvHistory(clock=Clock(1_700_000_000_000, 1_700_000_300_000))
        history.update(_report(node_id="bedroom", temp=20.0))
        history.update(_report(node_id="kitchen", temp=25.0))
        bundle = history.chart_bundle(node_id="bedroom")
        assert bundle.node_id == "bedroom"
        assert list(bundle.temp) == [200]

    def test_unknown_node_falls_back_to_the_newest(self):
        history = EnvHistory(clock=Clock(1_700_000_000_000))
        history.update(_report(node_id="bedroom", temp=20.0))
        assert history.chart_bundle(node_id="nope").node_id == "bedroom"

    def test_reset_clears_the_history(self):
        history = EnvHistory(clock=Clock(1_700_000_000_000))
        history.update(_report())
        assert history.chart_bundle() is not None
        history.reset()
        assert history.latest() is None
        assert history.chart_bundle() is None


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__] + sys.argv[1:]))
