"""Host tests for ``lma_core/sprout_history.py`` (the server's reply buffer)."""

from lma_core.sprout_history import (
    DATA_AIR_MAX_SAMPLES,
    DEFAULT_MAXLEN,
    SproutHistory,
)


class Reading:
    def __init__(self, sensor_id, value):
        self.sensor_id = sensor_id
        self.value = value
        self.unit = "%"


class Report:
    def __init__(self, node_id, readings):
        self.node_id = node_id
        self.readings = readings


def _report(
    node_id="e824ad2da00d2c8a45c2db6e700c989b",
    moisture=46.0,
    air_temp=26.0,
    air_humidity=55.0,
    dry=37.0,
    wet=53.0,
):
    return Report(
        node_id,
        [
            Reading(2, air_humidity),  # air humidity — the Sprout's SHT30
            Reading(3, air_temp),  # air temperature — ditto
            Reading(4, moisture),  # soil moisture
            Reading(10, dry),  # active profile dry threshold
            Reading(11, wet),  # active profile wet threshold
            Reading(99, 1.0),  # unknown id must be ignored
        ],
    )


class TestSproutHistory:
    def test_nothing_known_yet_yields_no_line(self):
        assert SproutHistory().data_line() == ""

    def test_keeps_moisture_and_band_and_formats_the_line(self):
        history = SproutHistory()
        assert history.update(_report(moisture=46.0)) is True
        history.update(_report(moisture=45.0))
        assert history.samples == [46.0, 45.0]
        assert history.temp == [26.0, 26.0]
        assert history.humidity == [55.0, 55.0]
        assert history.data_line() == "DATA e824ad2d 37 53 2 26 26 2 55 55 2 46 45"

    def test_band_defaults_to_minus_one_until_the_node_reports_it(self):
        history = SproutHistory()
        history.update(Report("e824ad2d", [Reading(4, 41.5)]))
        assert history.data_line() == "DATA e824ad2d -1 -1 0 0 1 42"

    def test_ring_keeps_only_the_newest_samples(self):
        history = SproutHistory(maxlen=3)
        for value in (10.0, 20.0, 30.0, 40.0):
            history.update(_report(moisture=value))
        assert history.samples == [20.0, 30.0, 40.0], "oldest dropped, order kept"
        assert history.data_line() == "DATA e824ad2d 37 53 3 26 26 26 3 55 55 55 3 20 30 40"

    def test_air_series_follow_their_own_readings(self):
        history = SproutHistory()
        history.update(_report(moisture=46.0, air_temp=25.7, air_humidity=58.0))
        history.update(_report(moisture=45.0, air_temp=26.3, air_humidity=59.5))
        assert history.temp == [25.7, 26.3]
        assert history.humidity == [58.0, 59.5]
        assert history.data_line() == "DATA e824ad2d 37 53 2 26 26 2 58 60 2 46 45"

    def test_air_series_are_capped_on_the_wire_while_moisture_keeps_its_ring(self):
        # LXMF OPPORTUNISTIC single-packet content is capped at 295 bytes
        # (beyond it the reply silently falls back to link delivery, which a
        # half-duplex LoRa leaf cannot serve).  Air history is supplementary, so
        # it is trimmed; the soil chart must keep its full depth.
        history = SproutHistory()
        for i in range(DEFAULT_MAXLEN + 5):
            history.update(_report(moisture=40.0 + i, air_temp=20.0 + i * 0.5, air_humidity=60.0 - i))
        assert len(history.samples) == DEFAULT_MAXLEN
        line = history.data_line()
        assert line.startswith(f"DATA e824ad2d 37 53 {len(history.temp[-DATA_AIR_MAX_SAMPLES:])}")
        # Re-parse: the emitted air counts must equal the wire cap and the
        # moisture count must equal the full ring length.
        parts = line.split()
        idx = 4
        ct = int(parts[idx]); idx += 1
        idx += ct
        ch = int(parts[idx]); idx += 1
        idx += ch
        cm = int(parts[idx])
        assert ct == DATA_AIR_MAX_SAMPLES, "temp trimmed to the wire cap"
        assert ch == DATA_AIR_MAX_SAMPLES, "humidity trimmed to the wire cap"
        assert cm == DEFAULT_MAXLEN, "moisture keeps its full ring"

    def test_air_only_report_does_not_adopt_the_history(self):
        # SHT30 readings alone (no moisture sample) must not adopt the history:
        # the Cardputer's own humidity shares sensor_id 2 with the Sprout, so
        # anything without a moisture reading is not a Sprout report.
        history = SproutHistory()
        history.update(_report(moisture=46.0))
        history.update(
            Report("sprout2", [Reading(2, 54.0), Reading(3, 25.0)])
        )
        assert history.temp == [26.0], "air values only kept with the owner node"
        assert history.humidity == [55.0]

    def test_node_id_is_truncated_for_the_line(self):
        history = SproutHistory()
        history.update(_report(node_id="abcdef0123456789", moisture=40.0))
        assert history.data_line().startswith("DATA abcdef01 ")

    def test_unknown_and_malformed_readings_are_ignored(self):
        history = SproutHistory()
        history.update(Report("e824ad2d", [Reading(7, 1.0), "not-a-reading"]))
        assert history.samples == []
        assert history.data_line() == ""

    def test_band_only_report_does_not_produce_a_line(self):
        history = SproutHistory()
        history.update(Report("e824ad2d", [Reading(10, 37.0), Reading(11, 53.0)]))
        assert history.data_line() == "", "no sampling data yet"

    def test_another_nodes_report_cannot_hijack_the_history(self):
        # Live regression: the Cardputer reports its own temperature/humidity
        # (sensor_id 1 + a humidity that shares the Sprout's id 2) every 60 s;
        # none of that may grow the Sprout's series or restamp the DATA line.
        history = SproutHistory()
        history.update(_report(moisture=46.0))
        history.update(
            Report("cardputer01", [Reading(1, 42.0), Reading(2, 55.0), Reading(3, 26.0)])
        )
        assert history.samples == [46.0], "other node's readings ignored"
        assert history.temp == [26.0], "other node's temperature/humidity readings never pushed"
        assert history.humidity == [55.0]
        assert history.data_line().startswith("DATA e824ad2d "), "node id unchanged"

    def test_a_moisture_report_from_a_second_node_takes_over(self):
        history = SproutHistory()
        history.update(_report(moisture=46.0))
        history.update(Report("secondnode", [Reading(4, 30.0)]))
        assert history.data_line().startswith("DATA secondno ")

    def test_reset_clears_everything(self):
        history = SproutHistory()
        history.update(_report())
        history.reset()
        assert history.samples == []
        assert history.temp == []
        assert history.humidity == []
        assert history.data_line() == ""


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__] + sys.argv[1:]))
