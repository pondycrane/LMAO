"""Unit tests for tools/calendar_pics_sync.py (pure logic; no network/DB)."""

import io
import sys
from pathlib import Path

import pytest
from PIL import Image

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from tools import calendar_pics_sync as cps  # noqa: E402


def _r(title, url=None, w=512, h=512):
    return (title, url or ("https://x/" + title), w, h)


class TestActionExtraction:
    def test_recognizes_activity_in_title(self):
        assert cps.extract_action("W ice skating 1:30") == "ice skating"
        assert cps.extract_action("J tennis 4pm Mon") == "playing tennis"
        assert cps.extract_action("Swim meet") == "swimming"
        assert cps.extract_action("Juliet dentist") == "at the dentist"
        assert cps.extract_action("W Ninja Warrior") == "doing martial arts"

    def test_longest_key_wins(self):
        # "ice skating" must beat the bare "skat"/"school" style substrings
        assert cps.extract_action("J ice skating lesson") == "ice skating"

    def test_school_synonyms_map_to_school(self):
        assert cps.extract_action("Wesley Princeton") == "at school"
        assert cps.extract_action("J early dismissal 1:10pm") == "at school"

    def test_no_activity_gives_empty(self):
        assert cps.extract_action("Parent team lunch") == ""


class TestPersonWord:
    def test_named_kids_get_their_own_kind(self):
        assert cps.person_word(["juliet"]) == "girl"
        assert cps.person_word(["wesley"]) == "boy"

    def test_shared_event_shows_kids_together(self):
        assert cps.person_word(["juliet", "wesley"]) == "kids"

    def test_unknown_participant_stays_neutral(self):
        assert cps.person_word([]) == "kid"
        assert cps.person_word(["alex"]) == "kid"


class TestBuildQueries:
    def test_activity_is_the_primary_query(self):
        c = cps.build_queries("J tennis 4pm Mon", ["juliet"])
        assert c["action"] == "playing tennis"
        assert c["queries"][0] == "girl playing tennis coloring page"
        assert c["queries"][0] in c["queries"]

    def test_shared_event_asks_for_kids(self):
        c = cps.build_queries("J and W Chinese Class", ["juliet", "wesley"])
        assert c["person"] == "kids"
        assert c["queries"][0].startswith("kids ")

    def test_activityless_event_falls_back_to_a_kid(self):
        c = cps.build_queries("Parent team lunch", ["wesley"])
        assert c["action"] == ""
        assert c["queries"][0] == "boy cartoon coloring page"

    def test_queries_are_deterministic_and_deduped(self):
        a = cps.build_queries("J tennis 4pm", ["juliet"])
        b = cps.build_queries("J tennis 4pm", ["juliet"])
        assert a == b
        assert len(a["queries"]) == len(set(a["queries"]))

    def test_no_model_hook_remains(self):
        # the stage is deterministic: no LLM/vision call may shape a query
        assert not hasattr(cps, "_llm_combo")
        assert not hasattr(cps, "verify_subject")
        assert not hasattr(cps, "crop_to_subject")


class TestLineArtGate:
    def _png(self, grid):
        h, w = len(grid), len(grid[0])
        im = Image.new("L", (w, h), 255)
        px = im.load()
        for y, row in enumerate(grid):
            for x, v in enumerate(row):
                if v:
                    px[x, y] = 0
        buf = io.BytesIO()
        im.point(lambda v: 255 if v >= 128 else 0, mode="1").save(buf, format="PNG")
        return buf.getvalue()

    def _outline_box(self, size=20, inset=2):
        """A hollow box: outline ink with a big enclosed white interior."""
        return [[1 if (x in (inset, size - 1 - inset) or y in (inset, size - 1 - inset))
                 else 0 for x in range(size)] for y in range(size)]

    def _solid_box(self, size=20, inset=2):
        """A filled silhouette: lots of ink, no enclosed white."""
        return [[1 if (inset <= x <= size - 1 - inset and inset <= y <= size - 1 - inset)
                 else 0 for x in range(size)] for y in range(size)]

    def test_outline_drawing_is_accepted(self):
        assert cps.is_line_art(self._png(self._outline_box()))

    def test_filled_silhouette_is_rejected(self):
        png = self._png(self._solid_box())
        assert cps.ink_fraction(png) > cps.LINE_ART_MIN_INK
        assert cps.enclosed_white_fraction(png) < cps.LINE_ART_MIN_ENCLOSED
        assert not cps.is_line_art(png)

    def test_blank_page_is_rejected(self):
        grid = [[0] * 20 for _ in range(20)]
        grid[9][9] = 1
        assert not cps.is_line_art(self._png(grid))

    def test_thresholded_photo_is_rejected(self):
        grid = [[1 if (x + y) % 17 else 0 for x in range(20)] for y in range(20)]
        assert not cps.is_line_art(self._png(grid))

    def test_measurements_match_pixels(self):
        png = self._png(self._outline_box())
        assert abs(cps.enclosed_white_fraction(png) - 196 / 400) < 1e-9
        assert abs(cps.ink_fraction(png) - 76 / 400) < 1e-9


class TestRasterizer:
    def test_thin_strokes_survive_downscale(self):
        """A 1-px outline stroke in a large source must still show at 176x176.

        The old path (LANCZOS downscale, then threshold) averaged thin strokes
        into gray and dropped them.
        """
        src = Image.new("L", (1760, 1760), 255)
        px = src.load()
        for y in range(100, 1700):
            for x in range(300, 1400):
                if x in (300, 1399) or y in (100, 1699):
                    px[x, y] = 0
        buf = io.BytesIO()
        src.save(buf, format="PNG")
        tile = cps.to_1bit_png(buf.getvalue(), 176, 176, cps._THRESHOLD)
        with Image.open(io.BytesIO(tile)) as im:
            im = im.convert("L")
            ink = [(x, y) for x in range(176) for y in range(176)
                   if im.getpixel((x, y)) < 128]
        xs = [x for x, _ in ink]
        ys = [y for _, y in ink]
        assert im.getpixel((min(xs), (min(ys) + max(ys)) // 2)) < 128
        assert im.getpixel(((min(xs) + max(xs)) // 2, min(ys))) < 128

    def test_portrait_drawing_keeps_its_proportions(self):
        """A tall coloring page must be fitted, not squashed to a square.

        The old square resize turned a 700x900 page into an unrecognizable
        blob; the aspect-preserving fit leaves the figure centered with white
        bars left and right.
        """
        src = Image.new("L", (700, 900), 255)
        px = src.load()
        for y in range(100, 800):        # a tall rectangle, 400 wide x 700 high
            for x in range(150, 550):
                if x in (150, 549) or y in (100, 799):
                    px[x, y] = 0
        buf = io.BytesIO()
        src.save(buf, format="PNG")
        tile = cps.to_1bit_png(buf.getvalue(), 176, 176, cps._THRESHOLD)
        with Image.open(io.BytesIO(tile)) as im:
            im = im.convert("L")
            ink = [(x, y) for x in range(176) for y in range(176)
                   if im.getpixel((x, y)) < 128]
        xs = [x for x, _ in ink]
        ys = [y for _, y in ink]
        content_w = max(xs) - min(xs) + 1
        content_h = max(ys) - min(ys) + 1
        # 400:700 content stays ~0.57 aspect (not 1.0), and it is centered
        assert 0.45 < content_w / content_h < 0.75
        assert abs((min(xs) + max(xs)) / 2 - 87.5) < 3
        assert min(xs) >= cps.TILE_MARGIN - 1
        assert max(ys) <= 176 - cps.TILE_MARGIN + 1

    def test_rasterizer_output_is_1bit(self):
        src = Image.new("L", (352, 352), 255)
        px = src.load()
        for x in range(40, 300):
            for y in (40, 300):
                px[x, y] = 0
        buf = io.BytesIO()
        src.save(buf, format="PNG")
        tile = cps.to_1bit_png(buf.getvalue(), 176, 176, cps._THRESHOLD)
        with Image.open(io.BytesIO(tile)) as im:
            assert im.mode == "1"
            assert im.size == (176, 176)

    def test_blank_source_yields_a_blank_tile(self):
        src = Image.new("L", (400, 400), 255)
        buf = io.BytesIO()
        src.save(buf, format="PNG")
        tile = cps.to_1bit_png(buf.getvalue(), 176, 176, cps._THRESHOLD)
        assert cps.ink_fraction(tile) == 0


class TestContentAspect:
    def test_measures_the_ink_bbox(self):
        src = Image.new("L", (400, 400), 255)
        px = src.load()
        for y in range(50, 150):  # 200 wide x 100 high ink -> aspect 2.0
            for x in range(50, 250):
                px[x, y] = 0
        buf = io.BytesIO()
        src.save(buf, format="PNG")
        assert abs(cps.content_aspect(buf.getvalue()) - 2.0) < 1e-6

    def test_blank_source_has_no_aspect(self):
        src = Image.new("L", (400, 400), 255)
        buf = io.BytesIO()
        src.save(buf, format="PNG")
        assert cps.content_aspect(buf.getvalue()) is None


class TestPickSource:
    def test_prefers_line_art_over_a_photo_of_the_same_subject(self):
        results = [_r("Girl playing tennis photo.jpg"),
                   _r("Girl playing tennis coloring page.png")]
        picked = cps.pick_source(results, "girl playing tennis coloring page")
        assert "coloring" in picked[0].lower()

    def test_prefers_a_single_kid_drawing_over_a_print_sheet(self):
        results = [_r("Tennis coloring pages for kids gallery.png"),
                   _r("Girl playing tennis coloring page.png")]
        picked = cps.pick_source(results, "girl playing tennis coloring page")
        assert "gallery" not in picked[0].lower()

    def test_rejects_giant_maps(self):
        results = [_r("Diagram of everything.png", w=9000, h=9000),
                   _r("Tennis court map.png", w=8000, h=6000),
                   _r("Girl playing tennis coloring page.png", w=512, h=282)]
        assert "tennis coloring" in cps.pick_source(
            results, "girl playing tennis coloring page")[0].lower()

    def test_rejects_svg_without_a_rasterizer(self):
        assert cps.pick_source([_r("Tennis line drawing.svg")],
                               "girl playing tennis") is None

    def test_rejects_unrelated_titles(self):
        # a query token must appear in the title
        assert cps.pick_source([_r("Dolphin logo.png")],
                               "girl playing tennis coloring page") is None


class TestWebSource:
    def test_no_local_generator_remains(self):
        # tiles are fetched line drawings; the ComfyUI generation path is gone
        assert not hasattr(cps, "generate_local")
        assert not hasattr(cps, "build_gen_prompt")

    def test_ddg_results_map_to_title_url_size(self, monkeypatch):
        monkeypatch.setattr(cps, "_ddg_vqd", lambda q: "4-123")

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"results":[{"title":"Girl tennis coloring page",' \
                       b'"image":"https://x/girl-tennis.png","width":1024,"height":1536},' \
                       b'{"title":"no url","image":"","width":1,"height":1}]}'

        monkeypatch.setattr(cps.urllib.request, "urlopen", lambda *a, **k: _Resp())
        got = cps.search_web_images("girl playing tennis coloring page")
        assert got == [("Girl tennis coloring page",
                        "https://x/girl-tennis.png", 1024, 1536)]

    def test_search_sources_issues_the_query_verbatim(self, monkeypatch):
        commons, web = [], []
        monkeypatch.setattr(cps, "search_commons", lambda q: commons.append(q) or [])
        monkeypatch.setattr(
            cps, "search_web_images",
            lambda q: web.append(q) or [("t", "https://x/a.png", 800, 800)])
        got = cps.search_sources("girl playing tennis coloring page")
        q = "girl playing tennis coloring page"
        # the activity query is used as-is, and only ONE web call is spent on it
        assert commons == [q]
        assert web == [q]
        assert len(got) == 1

    def test_search_sources_survives_a_web_search_failure(self, monkeypatch):
        monkeypatch.setattr(cps, "search_commons",
                            lambda q: [("Tennis.png", "https://x/d.png", 512, 512)])

        def boom(q):
            raise RuntimeError("rate limited")

        monkeypatch.setattr(cps, "search_web_images", boom)
        assert cps.search_sources("girl playing tennis") == [
            ("Tennis.png", "https://x/d.png", 512, 512)]


class TestBackfillWindow:
    def test_since_days_zero_is_future_only(self):
        import time

        cal = _FakeCal([])
        cps.kid_events_needing_pics(cal, _FakePics([]), force=True, since_days=0)
        cutoff = cal.queries[-1][1][0]
        assert abs(cutoff - time.time() * 1000) < 5000

    def test_since_days_includes_recent_past(self):
        import time

        cal = _FakeCal([])
        cps.kid_events_needing_pics(cal, _FakePics([]), force=True, since_days=14)
        cutoff = cal.queries[-1][1][0]
        assert abs(cutoff - (time.time() - 14 * 86400) * 1000) < 5000

    def test_force_ignores_already_stored_keys(self):
        rows = [("u1", "J tennis", "ev_1", ["juliet"]),
                ("u2", "W swim", "ev_2", ["wesley"])]
        pics = _FakePics(["ev_1"])
        assert cps.kid_events_needing_pics(_FakeCal(rows), pics) == [rows[1]]
        assert cps.kid_events_needing_pics(_FakeCal(rows), pics, force=True) == rows

    def test_limit_caps_the_batch(self):
        rows = [(f"u{i}", f"event {i}", f"ev_{i}", ["juliet"]) for i in range(5)]
        got = cps.kid_events_needing_pics(_FakeCal(rows), _FakePics([]),
                                          force=True, limit=2)
        assert got == rows[:2]


class _FakeCursor:
    def __init__(self, rows, queries):
        self._rows, self._queries = rows, queries

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._queries.append((sql, params))

    def fetchall(self):
        return self._rows


class _FakeCal:
    def __init__(self, rows):
        import threading

        self._lock, self._conn, self.queries = threading.Lock(), self, []
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows, self.queries)

    def close(self):
        pass


class _FakePics:
    def __init__(self, keys):
        self._keys = keys

    def keys(self):
        return self._keys

    def close(self):
        pass


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
