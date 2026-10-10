"""Unit tests for lma_core.calendar_pics (PicStore + pure helpers).

Covers the RLCD picture contract the agent-facing tool embodies:
- ``quantize_1bit`` / ``pick_tile_size`` semantics (pure, no imaging libs);
- ``PicStore`` upsert/get/keys against a scripted psycopg (same fake-module
  pattern as ``test_calendar_pg.py``).
"""

import sys
import types

import pytest

from lma_core.calendar_pics import (
    PicStore,
    pick_tile_size,
    quantize_1bit,
)

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_quantize_1bit_threshold(self):
        grid = [[10, 90, 128, 129, 255], [127, 0, 200, 255, 0]]
        out = quantize_1bit(grid, threshold=128)
        assert out == [
            [0, 0, 0, 255, 255],
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
    for key in list(sys.modules):
        if key == "lma_core.calendar_pics":
            del sys.modules[key]

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

    fake_module = types.ModuleType("psycopg")
    fake_module.connect = connect
    sys.modules["psycopg"] = fake_module

    # remove the already-imported module so re-import binds the fake psycopg
    for key in list(sys.modules):
        if key.startswith("lma_core.calendar_pics"):
            del sys.modules[key]
    import lma_core.calendar_pics as pics

    yield pics, captures, make_cursor

    for key in list(sys.modules):
        if key.startswith("lma_core.calendar_pics"):
            del sys.modules[key]
    del sys.modules["psycopg"]


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
            "swim", 144, 144, "image/png", b"\x89PNG...", 1700000000000, 1700000000000,
        )
        store._conn.cursor = staticmethod(lambda: cursor)

        rec = store.put("swim", b"\x89PNG...", 144, 144)

        assert rec["pic_key"] == "swim"
        assert rec["width"] == 144 and rec["height"] == 144
        assert rec["mime"] == "image/png"
        assert rec["bytes"] == b"\x89PNG..."
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
