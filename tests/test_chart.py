"""Host tests for ``cardputer_client/chart.py``.

The chart runs on the Cardputer under MicroPython, so its pure parts (the DATA
parser, the geometry) are tested here with a fake display that records pixels —
no hardware, no st7789.
"""

import chart


class FakeTft:
    """Records what the chart drew; mimics fill/line/pixel/text."""

    def __init__(self, with_line=True):
        self.pixels = {}
        self.lines = []
        self.texts = []
        self.fills = 0
        if not with_line:
            # The chart probes for the primitive, so an absent one is None.
            self.line = None

    def fill(self, color):
        self.fills += 1
        self.pixels.clear()
        self.lines = []

    def line(self, x0, y0, x1, y1, color):
        self.lines.append((x0, y0, x1, y1, color))

    def pixel(self, x, y, color):
        self.pixels[(x, y)] = color

    def text(self, string, x, y, color):
        self.texts.append((string, x, y, color))

    def colors(self):
        return {c for c in self.pixels.values()} | {c for *_, c in self.lines}

    def rows_with(self, color):
        rows = {y for (_, y), c in self.pixels.items() if c == color}
        rows |= {y0 for (_, y0, _, y1, c) in self.lines if c == color and y0 == y1}
        return rows


class TestParseDataLine:
    def test_parses_a_record_sharing_the_message_with_the_ack(self):
        msg = (
            "ACK from LMAO Server — received your message (89 bytes)\n"
            "DATA e824ad2d 37 53 2 25 26 2 54 55 3 46 45 47"
        )
        data = chart.parse_data_line(msg)
        assert data["node"] == "e824ad2d"
        assert data["dry"] == 37
        assert data["wet"] == 53
        assert data["samples"] == [46, 45, 47], "oldest first, order preserved"

    def test_parses_all_three_series_with_their_counts(self):
        data = chart.parse_data_line("DATA e824ad2d 37 53 3 25 26 27 2 54 55 3 40 41 42")
        assert data["temp"] == [25.0, 26.0, 27.0], "temperature kept as °C floats"
        assert data["humidity"] == [54, 55], "humidity as integer percent"
        assert data["samples"] == [40, 41, 42]

    def test_parses_empty_series_counts(self):
        data = chart.parse_data_line("DATA e824ad2d -1 -1 0 0 1 42")
        assert data["dry"] == -1
        assert data["wet"] == -1
        assert data["temp"] == []
        assert data["humidity"] == []
        assert data["samples"] == [42]

    def test_old_moisture_only_format_is_ignored(self):
        # A line in the pre-air format would misparse the first count field;
        # it must be ignored rather than drawn as garbage — the server and the
        # Cardputer are flashed together, so this only happens mid-deploy.
        assert chart.parse_data_line("DATA e824ad2d 37 53 40 41 42") is None

    def test_temp_only_line_is_rejected(self):
        # Air is only ever emitted alongside moisture, so a percent-less line
        # (temperature alone) is not a valid record — reaching draw() would
        # otherwise blank the chart.
        assert chart.parse_data_line("DATA e824ad2d 37 53 2 25 26 0 0") is None

    def test_reports_unknown_band_as_minus_one(self):
        data = chart.parse_data_line("DATA e824ad2d -1 -1 0 0 2 40 41")
        assert data["dry"] == -1
        assert data["wet"] == -1
        assert data["samples"] == [40, 41]

    def test_rounds_fractional_values(self):
        data = chart.parse_data_line("DATA e824ad2d 36.5 53.4 1 26.4 1 60.5 1 46.6")
        assert (data["dry"], data["wet"]) == (36, 53)
        assert data["temp"] == [26.4]
        assert data["humidity"] == [60]
        assert data["samples"] == [47]

    def test_ignores_non_data_traffic(self):
        assert chart.parse_data_line("ACK from LMAO Server") is None
        assert chart.parse_data_line("") is None
        assert chart.parse_data_line(None) is None

    def test_ignores_malformed_records(self):
        assert chart.parse_data_line("DATA e824ad2d 37") is None
        assert chart.parse_data_line("DATA e824ad2d 37 53 1 notanumber") is None
        assert chart.parse_data_line("DATA e824ad2d 37 53 0") is None, "no series"
        assert chart.parse_data_line("DATA e824ad2d 37 53 1 26 2 55") is None, (
            "moisture count missing"
        )
        assert chart.parse_data_line("DATA e824ad2d 37 53 1 26 1 55 1 40 999") is None, (
            "trailing garbage"
        )


class TestGeometry:
    def test_window_always_contains_the_band(self):
        lo, hi = chart.y_window(37, 53, [46, 46, 46])
        assert lo <= 37 and hi >= 53

    def test_window_keeps_a_minimum_span_for_a_flat_series(self):
        lo, hi = chart.y_window(37, 53, [46, 46])
        assert hi - lo >= 20

    def test_window_is_clamped_to_the_sensor_range(self):
        lo, hi = chart.y_window(0, 0, [0, 1])
        assert lo >= 0
        lo, hi = chart.y_window(100, 100, [99, 100])
        assert hi <= 100

    def test_higher_moisture_draws_higher_on_screen(self):
        assert chart.y_px(60, 0, 100) < chart.y_px(40, 0, 100)
        assert chart.y_px(100, 0, 100) == chart.PLOT_T
        assert chart.y_px(0, 0, 100) == chart.PLOT_B

    def test_x_spans_the_plot_box(self):
        assert chart.x_px(0, 10) == chart.PLOT_L
        assert chart.x_px(9, 10) == chart.PLOT_R
        assert chart.x_px(0, 1) == chart.PLOT_L

    def test_temp_window_pads_and_wraps_the_values(self):
        lo, hi = chart.temp_window([20, 21, 22])
        assert lo <= 20 and hi >= 22, "window covers every temperature"
        assert hi - lo >= 4, "flat series keeps a readable span"

    def test_temp_window_handles_a_flat_or_single_value(self):
        lo, hi = chart.temp_window([21.5])
        assert lo < 21.5 < hi
        assert hi - lo >= 4

    def test_temp_window_handles_negative_values(self):
        lo, hi = chart.temp_window([-3, -1])
        assert lo <= 100
        assert lo <= -3 and hi >= -1

    def test_empty_window_returns_the_default_range(self):
        # Defense in depth: draw() always passes at least one percent series,
        # but an empty window must not crash the min()/max() computation.
        assert chart.y_window(None, None) == (0, 100)


class TestDraw:
    def _data(self, **kw):
        base = {"node": "e824ad2d", "dry": 37, "wet": 53, "samples": [46, 45, 47, 46]}
        base.update(kw)
        return base

    def test_draws_frame_thresholds_and_trace(self):
        tft = FakeTft()
        result = chart.draw(tft, self._data())
        assert result["error"] is None
        assert tft.fills == 1, "clears the screen first"
        assert chart.DRY_COLOR in tft.colors(), "dry line drawn"
        assert chart.WET_COLOR in tft.colors(), "wet line drawn"
        assert chart.GREY in tft.colors(), "frame drawn"
        assert result["points"] == 3, "three trace segments for four samples"

    def test_uses_the_displays_line_primitive_when_available(self):
        tft = FakeTft(with_line=True)
        chart.draw(tft, self._data())
        assert len(tft.lines) >= 4, "frame + thresholds + trace via line()"
        assert tft.pixels, "only the newest-sample marker uses pixel()"

    def test_falls_back_to_pixels_without_a_line_primitive(self):
        tft = FakeTft(with_line=False)
        result = chart.draw(tft, self._data())
        assert result["error"] is None
        assert tft.lines == []
        assert chart.DRY_COLOR in tft.colors(), "still drawn, pixel by pixel"
        assert len(tft.pixels) > 100, "frame + thresholds + trace as pixels"

    def test_threshold_lines_sit_on_their_values(self):
        tft = FakeTft()
        data = self._data()
        chart.draw(tft, data)
        lo, hi = chart.y_window(data["dry"], data["wet"], data["samples"])
        assert chart.y_px(data["dry"], lo, hi) in tft.rows_with(chart.DRY_COLOR)
        assert chart.y_px(data["wet"], lo, hi) in tft.rows_with(chart.WET_COLOR)

    def test_threshold_values_are_labeled_in_their_own_colour(self):
        tft = FakeTft()
        chart.draw(tft, self._data())
        dry_labels = [t[0] for t in tft.texts if t[3] == chart.DRY_COLOR]
        wet_labels = [t[0] for t in tft.texts if t[3] == chart.WET_COLOR]
        assert "37" in dry_labels, "dry line carries its number"
        assert "53" in wet_labels, "wet line carries its number"

    def test_newest_sample_is_labeled(self):
        tft = FakeTft()
        data = self._data()
        chart.draw(tft, data)
        white_labels = [t[0] for t in tft.texts if t[3] == chart.WHITE]
        assert str(data["samples"][-1]) in white_labels, (
            "newest value drawn next to the marker"
        )

    def test_labels_stay_inside_the_plot_box(self):
        tft = FakeTft()
        chart.draw(tft, self._data(dry=0, wet=100))
        for _, x, y, _ in tft.texts:
            assert x >= 0, "no label left of the screen"
            assert x <= chart.W - 8, "no label right of the screen"
            assert y >= 0, "no label above the screen"
            assert y <= chart.H - 2, "footer/labels stay on screen"

    def test_labels_show_the_latest_reading_and_the_band(self):
        tft = FakeTft()
        data = self._data()
        chart.draw(tft, data)
        rendered = " | ".join(t[0] for t in tft.texts)
        assert f"{data['samples'][-1]}%" in rendered, "latest sample in the header"
        assert "dry 37" in rendered and "wet 53" in rendered
        assert "n=4" in rendered

    def test_missing_band_draws_no_threshold_lines(self):
        tft = FakeTft()
        chart.draw(tft, self._data(dry=-1, wet=-1))
        assert chart.DRY_COLOR not in tft.colors()
        assert chart.WET_COLOR not in tft.colors()
        rendered = " | ".join(t[0] for t in tft.texts)
        assert "dry --" in rendered and "wet --" in rendered

    def test_draws_humidity_and_temperature_traces(self):
        tft = FakeTft()
        data = self._data(humidity=[50, 60, 70, 55], temp=[20, 21, 22, 23])
        result = chart.draw(tft, data)
        assert result["error"] is None
        assert chart.HUM_COLOR in tft.colors(), "humidity trace drawn in green"
        assert chart.TEMP_COLOR in tft.colors(), "temperature trace drawn in orange"
        assert result["points"] == 9, "three line segments per trace for four samples"

    def test_humidity_shares_the_percent_axis(self):
        tft = FakeTft()
        data = self._data(humidity=[50, 60, 70, 55], temp=[20, 21, 22, 23])
        chart.draw(tft, data)
        lo, hi = chart.y_window(data["dry"], data["wet"], data["samples"], data["humidity"])
        # Newest humidity sample y equals where the percent axis maps it.
        row = tft.rows_with(chart.HUM_COLOR)
        assert chart.y_px(data["humidity"][-1], lo, hi) in row

    def test_temperature_uses_its_own_axis_with_right_gutter_labels(self):
        tft = FakeTft()
        data = self._data(humidity=[50, 60, 70, 55], temp=[20, 21, 22, 23])
        chart.draw(tft, data)
        tlo, thi = chart.temp_window(data["temp"])
        labels = [t[0] for t in tft.texts if t[3] == chart.TEMP_COLOR]
        assert str(int(round(thi))) in labels and str(int(round(tlo))) in labels, (
            "temperature scale labelled in its own colour"
        )
        for _, x, _, _ in tft.texts:
            assert x <= chart.W - 8, "labels stay right of the plot box still on screen"

    def test_header_shows_the_latest_air_readings(self):
        tft = FakeTft()
        data = self._data(humidity=[50, 60, 70, 55], temp=[20, 21, 22, 23])
        chart.draw(tft, data)
        rendered = " | ".join(t[0] for t in tft.texts)
        assert "HUM 55%" in rendered, "newest humidity in the header"
        assert "AIR 23C" in rendered, "newest temperature in the header (integer °C)"

    def test_missing_air_series_still_draws_the_soil_chart(self):
        tft = FakeTft()
        result = chart.draw(tft, self._data())
        assert result["error"] is None
        rendered = " | ".join(t[0] for t in tft.texts)
        assert "HUM --" in rendered and "AIR --" in rendered, "air placeholders"
        assert tft.colors() and chart.SOIL_COLOR in tft.colors()

    def test_temperature_trace_is_visible_even_when_flat(self):
        # Live regression: the Sprout's temperature is often constant (e.g. all
        # 24.0°C), so a 1px orange line is invisible against the axis and reads
        # as "no temperature". The trace must be drawn thick AND tagged with its
        # °C value so a flat reading is still visibly the temperature line.
        tft = FakeTft()
        data = self._data(humidity=[60] * 5, temp=[24.0] * 5)
        result = chart.draw(tft, data)
        assert result["error"] is None
        orange_segs = [l for l in tft.lines if l[4] == chart.TEMP_COLOR]
        assert len(orange_segs) >= 2 * 4, "temperature trace drawn thick (y and y+1)"
        orange_labels = [t[0] for t in tft.texts if t[3] == chart.TEMP_COLOR]
        assert "24C" in orange_labels, "flat temperature still labelled in orange"

    def test_single_sample_waits_instead_of_dividing_by_zero(self):
        tft = FakeTft()
        result = chart.draw(tft, self._data(samples=[46]))
        assert result["error"] is None
        assert any("waiting" in t[0] for t in tft.texts)

    def test_display_errors_are_reported_not_raised(self):
        class BrokenTft:
            def fill(self, color):
                raise OSError("spi wedged")

            def line(self, x0, y0, x1, y1, color):
                raise OSError("spi wedged")

            def pixel(self, x, y, color):
                raise OSError("spi wedged")

            def text(self, string, x, y, color):
                raise OSError("spi wedged")

        result = chart.draw(BrokenTft(), self._data())
        assert result["error"] is not None
        assert "OSError" in result["error"]

    def test_no_display_is_not_an_error_to_raise(self):
        assert chart.draw(None, self._data())["error"] == "no display"


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__] + sys.argv[1:]))
