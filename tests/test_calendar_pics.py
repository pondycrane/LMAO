"""Unit tests for lma_core.calendar_pics (PicStore + pure helpers).

Covers the RLCD picture contract the agent-facing tool embodies:
- ``quantize_1bit`` / ``pick_tile_size`` semantics (pure, no imaging libs);
- ``rasterize_line_art`` — the ONE aspect-preserving line-drawing rasterizer
  both the hourly sync stage and the by-hand tool use;
- ``PicStore`` upsert/get/keys against a scripted psycopg (same fake-module
  pattern as ``test_calendar_pg.py``).
"""

import io
import sys
import types

import pytest

from PIL import Image

from lma_core.calendar_pics import (
    MAX_CONTENT_ASPECT,
    TILE_MARGIN,
    PicStore,
    content_aspect,
    pick_tile_size,
    quantize_1bit,
    rasterize_line_art,
    subject_mask,
)

# A real tiny 1-bit PNG so the PicStore tile derivation (Pillow) succeeds.
_TINY_BUF = io.BytesIO()
Image.new("1", (4, 4), 0).save(_TINY_BUF, format="PNG")
TINY_PNG = _TINY_BUF.getvalue()

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_quantize_1bit_threshold(self):
        grid = [[10, 90, 128, 129, 255], [127, 0, 200, 255, 0]]
        out = quantize_1bit(grid, threshold=128)
        # >= threshold -> white (same rule as the tool's _pil_to_1bit_png)
        assert out == [
            [0, 0, 255, 255, 255],
            [0, 0, 255, 255, 0],
        ]
        # a "white" line artwork: only values below threshold become ink
        assert quantize_1bit([[0], [255]]) == [[0], [255]]

    def test_pick_tile_size_stays_below_panel(self):
        w, h = pick_tile_size()
        assert (w, h) == (176, 176)
        # never overflow a smaller panel
        assert pick_tile_size(screen_w=100, screen_h=200) == (80, 176)


class TestRasterizer:
    """A square tile from a non-square source must keep the drawing's shape."""

    def _png(self, w, h, draw):
        im = Image.new("L", (w, h), 255)
        px = im.load()
        draw(px)
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()

    def _tile(self, raw):
        return Image.open(io.BytesIO(rasterize_line_art(raw))).convert("L")

    def _ink_bbox(self, im):
        xs, ys = [], []
        for x in range(im.width):
            for y in range(im.height):
                if im.getpixel((x, y)) < 128:
                    xs.append(x)
                    ys.append(y)
        return min(xs), min(ys), max(xs), max(ys)

    def test_portrait_source_is_fitted_not_squashed(self):
        def tall(px):
            for y in range(100, 800):
                for x in range(150, 550):
                    if x in (150, 549) or y in (100, 799):
                        px[x, y] = 0

        im = self._tile(self._png(700, 900, tall))
        x1, y1, x2, y2 = self._ink_bbox(im)
        w, h = x2 - x1 + 1, y2 - y1 + 1
        # 400:700 content stays tall (not a 1:1 square), centered, inside margin
        assert 0.45 < w / h < 0.75
        assert abs((x1 + x2) / 2 - (im.width - 1) / 2) < 3
        assert x1 >= TILE_MARGIN - 1
        assert y2 <= im.height - TILE_MARGIN + 1

    def test_wide_source_is_fitted_not_squashed(self):
        def wide(px):
            for y in range(40, 120):
                for x in range(20, 380):
                    if x in (20, 379) or y in (40, 119):
                        px[x, y] = 0

        im = self._tile(self._png(400, 200, wide))
        x1, y1, x2, y2 = self._ink_bbox(im)
        w, h = x2 - x1 + 1, y2 - y1 + 1
        # 360:80 content stays wide (not a 1:1 square), vertically centered
        assert 4.0 < w / h < 5.0
        assert abs((y1 + y2) / 2 - (im.height - 1) / 2) < 3

    def test_thin_strokes_survive_a_big_downscale(self):
        """A 1-px outline stroke must still be ink after a 10x downscale.

        The old path (LANCZOS downscale, then threshold) averaged thin strokes
        into gray and dropped them, which is why the tiles looked like blobs.
        """
        def figure(px):
            for y in range(100, 1700):
                for x in range(300, 1400):
                    if x in (300, 1399) or y in (100, 1699):
                        px[x, y] = 0

        im = self._tile(self._png(1760, 1760, figure))
        x1, y1, x2, y2 = self._ink_bbox(im)
        # the outline is still a closed rectangle at tile size
        assert im.getpixel((x1, (y1 + y2) // 2)) < 128
        assert im.getpixel(((x1 + x2) // 2, y1)) < 128
        assert im.getpixel(((x1 + x2) // 2, (y1 + y2) // 2)) == 255

    def test_output_is_a_1bit_tile(self):
        def box(px):
            for x in range(40, 300):
                for y in (40, 300):
                    px[x, y] = 0

        with Image.open(io.BytesIO(rasterize_line_art(
                self._png(352, 352, box), 176, 176))) as im:
            assert im.mode == "1"
            assert im.size == (176, 176)

    def test_blank_source_yields_a_blank_tile(self):
        tile = rasterize_line_art(self._png(400, 400, lambda px: None))
        with Image.open(io.BytesIO(tile)) as im:
            assert im.convert("L").getextrema() == (255, 255)

    def test_trim_drops_the_page_border(self):
        # a small drawing on a big white page fills the tile after the trim
        def dot(px):
            for y in range(140, 260):
                for x in range(140, 260):
                    px[x, y] = 0

        im = self._tile(self._png(400, 400, dot))
        x1, y1, x2, y2 = self._ink_bbox(im)
        assert (x2 - x1 + 1) > 0.5 * im.width
        assert (y2 - y1 + 1) > 0.5 * im.height

    def test_page_furniture_is_dropped(self):
        # a footer logo / page number is a small component: it must not ride
        # along in the trim and land in the tile as a black blob
        def figure_and_footer(px):
            for y in range(60, 340):        # the drawing
                for x in range(60, 260):
                    if x in (60, 259) or y in (60, 339):
                        px[x, y] = 0
            for y in range(370, 384):       # a footer bar
                for x in range(300, 380):
                    px[x, y] = 0

        raw = self._png(400, 400, figure_and_footer)
        mask, bbox = subject_mask(raw)
        assert bbox is not None
        assert bbox[3] <= 345  # footer excluded
        im = self._tile(raw)
        x1, y1, x2, y2 = self._ink_bbox(im)
        assert y2 - y1 + 1 > 0.5 * im.height  # the figure fills the tile

    def test_content_aspect_reads_the_subject_bbox(self):
        def strip(px):
            for y in range(50, 150):
                for x in range(50, 250):
                    px[x, y] = 0

        assert abs(content_aspect(self._png(400, 400, strip)) - 2.0) < 1e-6
        assert content_aspect(self._png(400, 400, lambda px: None)) is None

    def test_a_lone_hairline_is_furniture_not_a_drawing(self):
        # a 1-px line with nothing around it is a page rule, not artwork
        def line(px):
            for y in range(400):
                px[200, y] = 0

        mask, bbox = subject_mask(self._png(400, 400, line))
        assert mask is None and bbox is None

    def test_strip_limit_is_enforced_by_the_caller(self):
        assert MAX_CONTENT_ASPECT > 1


# ---------------------------------------------------------------------------
# PicStore against a scripted psycopg
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_pg():
    captures = {"executes": [], "dsn": None}

    def make_cursor():
        class _FakeCursor:
            def __init__(self):
                self.fetchone_value = None
                self.fetchall_rows = []

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=None):
                captures["executes"].append((sql, params))

            def fetchone(self):
                return self.fetchone_value

            def fetchall(self):
                return self.fetchall_rows

        return _FakeCursor()

    def connect(dsn, autocommit=False):
        captures["dsn"] = dsn
        return type(
            "_FakeConn",
            (),
            {"cursor": staticmethod(make_cursor), "close": lambda self: None},
        )()

    # Keep the ALREADY-imported calendar_pics module (the one the module-scope
    # PicStore below is bound to) stable — never re-import it across tests, or
    # fresh module objects leave the test-held class reading the real psycopg.
    # PicStore.__init__/open_pic_store read the `psycopg` global at call time,
    # so swapping it on the stable module is all that is needed (a pre-existing
    # "failed to resolve host 'pg'" CI failure).
    import lma_core.calendar_pics as pics

    real_psycopg = sys.modules.get("psycopg")
    fake_module = types.ModuleType("psycopg")
    fake_module.connect = connect
    sys.modules["psycopg"] = fake_module
    pics.psycopg = fake_module
    real_dsn = pics.compose_postgres_dsn

    yield pics, captures, make_cursor

    # Restore the module globals and the real psycopg for later imports.
    pics.psycopg = real_psycopg
    pics.compose_postgres_dsn = real_dsn
    if real_psycopg is not None:
        sys.modules["psycopg"] = real_psycopg
    else:
        sys.modules.pop("psycopg", None)


class TestPicStore:
    def test_init_ensures_schema(self, mock_pg):
        pics, captures, _ = mock_pg
        PicStore("postgresql://lmao:pw@pg:5432/lmao")
        assert any("CREATE TABLE IF NOT EXISTS cal_pics" in s for s, _ in captures["executes"])
        assert captures["dsn"].endswith("/lmao")

    def test_put_upserts_and_roundtrips(self, mock_pg):
        pics, captures, make_cursor = mock_pg
        store = PicStore("postgresql://lmao:pw@pg:5432/lmao")
        cursor = make_cursor()
        cursor.fetchone_value = (
            "swim", 176, 176, "image/png", TINY_PNG, None,
            1700000000000, 1700000000000,
        )
        store._conn.cursor = staticmethod(lambda: cursor)

        rec = store.put("swim", TINY_PNG, 176, 176)

        assert rec["pic_key"] == "swim"
        assert rec["width"] == 176 and rec["height"] == 176
        assert rec["mime"] == "image/png"
        assert rec["bytes"] == TINY_PNG
        # upsert carries ON CONFLICT
        assert any("ON CONFLICT(pic_key)" in s for s, _ in captures["executes"])
        assert [p for s, p in captures["executes"] if s.lstrip().startswith("INSERT")][0][0] == "swim"

    def test_get_missing_returns_none(self, mock_pg):
        pics, _, make_cursor = mock_pg
        store = PicStore("postgresql://lmao:pw@pg:5432/lmao")
        cursor = make_cursor()
        cursor.fetchone_value = None
        store._conn.cursor = staticmethod(lambda: cursor)
        assert store.get("nope") is None

    def test_open_none_without_dsn(self, mock_pg):
        pics, _, _ = mock_pg
        pics.compose_postgres_dsn = lambda: None
        assert pics.open_pic_store() is None

    def test_import_error_without_psycopg(self, mock_pg):
        pics, _, _ = mock_pg
        pics.psycopg = None
        with pytest.raises(ImportError, match="psycopg"):
            PicStore("postgresql://lmao:pw@pg:5432/lmao")
