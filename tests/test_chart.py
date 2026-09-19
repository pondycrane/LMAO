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
            "ACK from LMAO Server — received your message (89 bytes)\nDATA e824ad2d 37 53 46 45 47"
        )
        data = chart.parse_data_line(msg)
        assert data["node"] == "e824ad2d"
        assert data["dry"] == 37
        assert data["wet"] == 53
        assert data["samples"] == [46, 45, 47], "oldest first, order preserved"

    def test_reports_unknown_band_as_minus_one(self):
        data = chart.parse_data_line("DATA e824ad2d -1 -1 40 41")
        assert data["dry"] == -1
        assert data["wet"] == -1
        assert data["samples"] == [40, 41]

    def test_rounds_fractional_values(self):
        data = chart.parse_data_line("DATA e824ad2d 36.5 53.4 46.6")
        assert (data["dry"], data["wet"], data["samples"]) == (36, 53, [47])

    def test_ignores_non_data_traffic(self):
        assert chart.parse_data_line("ACK from LMAO Server") is None
        assert chart.parse_data_line("") is None
        assert chart.parse_data_line(None) is None

    def test_ignores_malformed_records(self):
        assert chart.parse_data_line("DATA e824ad2d 37") is None
        assert chart.parse_data_line("DATA e824ad2d 37 53 notanumber") is None
        assert chart.parse_data_line("DATA e824ad2d 37 53") is None, "no samples"


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
        chart.draw(tft, self._data())
        white_labels = [t[0] for t in tft.texts if t[3] == chart.WHITE]
        assert "47%" in white_labels or "47" in white_labels, (
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
        chart.draw(tft, self._data())
        rendered = " | ".join(t[0] for t in tft.texts)
        assert "47%" in rendered, "latest sample in the header"
        assert "dry 37" in rendered and "wet 53" in rendered
        assert "n=4" in rendered

    def test_missing_band_draws_no_threshold_lines(self):
        tft = FakeTft()
        chart.draw(tft, self._data(dry=-1, wet=-1))
        assert chart.DRY_COLOR not in tft.colors()
        assert chart.WET_COLOR not in tft.colors()
        rendered = " | ".join(t[0] for t in tft.texts)
        assert "dry --" in rendered and "wet --" in rendered

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
