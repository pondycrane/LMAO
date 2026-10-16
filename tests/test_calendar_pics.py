"""Unit tests for lma_core.calendar_pics (PicStore + pure helpers).

Covers the RLCD picture contract the agent-facing tool embodies:
- ``quantize_1bit`` / ``pick_tile_size`` semantics (pure, no imaging libs);
- ``PicStore`` upsert/get/keys against a scripted psycopg (same fake-module
  pattern as ``test_calendar_pg.py``).
"""

import io
import sys
import types

import pytest

from PIL import Image

from lma_core.calendar_pics import (
    PicStore,
    pick_tile_size,
    quantize_1bit,
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
        assert (w, h) == (144, 144)
        # never overflow a smaller panel
        assert pick_tile_size(screen_w=100, screen_h=200) == (80, 144)


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
            "swim", 144, 144, "image/png", TINY_PNG, None,
            1700000000000, 1700000000000,
        )
        store._conn.cursor = staticmethod(lambda: cursor)

        rec = store.put("swim", TINY_PNG, 144, 144)

        assert rec["pic_key"] == "swim"
        assert rec["width"] == 144 and rec["height"] == 144
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
