"""Host tests for ``lma_core/sprout_history.py`` (the server's reply buffer)."""

from lma_core.sprout_history import SproutHistory


class Reading:
    def __init__(self, sensor_id, value):
        self.sensor_id = sensor_id
        self.value = value
        self.unit = "%"


class Report:
    def __init__(self, node_id, readings):
        self.node_id = node_id
        self.readings = readings


def _report(node_id="e824ad2da00d2c8a45c2db6e700c989b", moisture=46.0, dry=37.0, wet=53.0):
    return Report(
        node_id,
        [
            Reading(2, 55.0),  # humidity  — not ours to keep
            Reading(3, 26.0),  # air temp  — ditto
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
        assert history.data_line() == "DATA e824ad2d 37 53 46 45"

    def test_band_defaults_to_minus_one_until_the_node_reports_it(self):
        history = SproutHistory()
        history.update(Report("e824ad2d", [Reading(4, 41.5)]))
        assert history.data_line() == "DATA e824ad2d -1 -1 42"

    def test_ring_keeps_only_the_newest_samples(self):
        history = SproutHistory(maxlen=3)
        for value in (10.0, 20.0, 30.0, 40.0):
            history.update(_report(moisture=value))
        assert history.samples == [20.0, 30.0, 40.0], "oldest dropped, order kept"
        assert history.data_line() == "DATA e824ad2d 37 53 20 30 40"

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
        assert history.data_line() == "", "no moisture sample yet"

    def test_another_nodes_report_cannot_hijack_the_history(self):
        # Live regression: the Cardputer reports its own temperature/humidity
        # every 60 s; those must not adopt the Sprout's series (the DATA line
        # would then carry the Cardputer's id).
        history = SproutHistory()
        history.update(_report(moisture=46.0))
        history.update(
            Report("cardputer01", [Reading(1, 42.0), Reading(2, 55.0), Reading(3, 26.0)])
        )
        assert history.samples == [46.0], "other node's readings ignored"
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
        assert history.data_line() == ""
