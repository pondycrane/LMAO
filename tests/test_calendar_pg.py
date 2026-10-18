"""Unit tests for lma_core.calendar_pg.CalendarStore (mocked psycopg).

Uses ``sys.modules`` mocking (same pattern as ``test_storage.py`` /
``test_queue.py``) so the store can be imported and exercised without a live
Postgres.  The fake psycopg module records every SQL statement and returns
scripted cursor results, so we can assert both the ensure-at-init DDL
(table + triggers) and the sync query shape without a database.
"""

import json
import sys
import types
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Fixture — fake psycopg module bound into calendar_pg
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_pg():
    """Bind calendar_pg's ``psycopg`` to a scripted fake and return hooks.

    ``cal_module`` is the freshly-imported ``lma_core.calendar_pg`` module;
    ``captures`` receives every ``execute(sql, params)`` the store issued;
    ``make_cursor`` lets each test script the watermark fetch-one value and
    the event rows (``fetchall``) the next sync should return.
    """
    for key in list(sys.modules):
        if key == "lma_core.calendar_pg":
            del sys.modules[key]

    captures = {"executes": [], "dsn": None, "closed": False}

    def make_cursor():
        class _FakeCursor:
            def __init__(self):
                self.fetchone_value = (1000,)  # watermark (epoch ms)
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
            {"cursor": staticmethod(make_cursor), "close": lambda self: setattr(captures, "closed", True)},
        )()

    fake_module = types.ModuleType("psycopg")
    fake_module.connect = connect
    sys.modules["psycopg"] = fake_module

    import lma_core.calendar_pg as cal  # noqa: E402  (re-imports vs. fake psycopg)

    yield cal, captures, make_cursor

    for key in list(sys.modules):
        if key == "lma_core.calendar_pg":
            del sys.modules[key]
    del sys.modules["psycopg"]


def _make_store(cal):
    return cal.CalendarStore("postgresql://lmao:pw@postgres.default:5432/lmao")


# ---------------------------------------------------------------------------
# Schema / initialization
# ---------------------------------------------------------------------------


class TestInit:
    def test_ensures_table_and_triggers(self, mock_pg):
        cal, captures, _ = mock_pg
        _make_store(cal)

        sql = " ".join(s for s, _ in captures["executes"])
        # table + index
        assert "CREATE TABLE IF NOT EXISTS cal_events" in sql
        assert "cal_events_cal_updated" in sql
        # bookkeeping + tombstone triggers
        assert "lmao_cal_touch" in sql and "lmao_cal_touch_trg" in sql
        assert "lmao_cal_tombstone" in sql and "lmao_cal_tombstone_trg" in sql
        # participants column is part of the schema and the in-place migration
        assert "participants JSONB NOT NULL DEFAULT '[]'" in sql
        assert "ADD COLUMN IF NOT EXISTS participants JSONB NOT NULL DEFAULT '[]'" in sql
        # DSN is the shared contact-book Postgres
        assert captures["dsn"] == "postgresql://lmao:pw@postgres.default:5432/lmao"

    def test_import_error_without_psycopg(self, mock_pg):
        cal, _, _ = mock_pg
        cal.psycopg = None
        with pytest.raises(ImportError, match="psycopg"):
            _make_store(cal)


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


class TestSync:
    def test_returns_mapped_events_and_watermark(self, mock_pg):
        cal, captures, make_cursor = mock_pg
        store = _make_store(cal)
        cursor = make_cursor()
        # fresh module import created one cursor during __init__; the fixture
        # cursor above is used for the sync (CalendarStore reuses the conn).
        old_executes = list(captures["executes"])
        cursor.fetchall_rows = [
            ("u1", "family", "Swim class", "bring towel", 1700000000001, 1700000000061, False, "swim", ["juliet"]),
            ("u2", "family", "Dentist", "", 1700000001000, 1700000001001, True, "dentist", []),
        ]
        # point the store's connection at this scripted cursor for the sync
        store._conn.cursor = staticmethod(lambda: cursor)

        events, watermark = store.sync(calendar_id="family", since_ms=500, max_events=10)

        assert watermark == 1000
        assert len(events) == 2
        assert events[0] == {
            "uid": "u1", "calendar_id": "family", "title": "Swim class",
            "notes": "bring towel", "start_ms": 1700000000001,
            "end_ms": 1700000000061, "deleted": False, "pic": "swim",
            "participants": ["juliet"],
        }
        assert events[1]["deleted"] is True and events[1]["pic"] == "dentist"
        assert events[1]["participants"] == []
        # the sync issues the watermark SELECT then the delta SELECT
        select_params = [p for s, p in captures["executes"] if "FROM cal_events" in s]
        assert [list(p) for p in select_params] == [["family", 500, 11]]
        # no execute happened after the query (two executes total for this sync)
        sync_execs = captures["executes"][len(old_executes):]
        assert len(sync_execs) == 2

    def test_truncates_window_over_max_events(self, mock_pg, caplog):
        import logging

        cal, captures, make_cursor = mock_pg
        store = _make_store(cal)
        cursor = make_cursor()
        store._conn.cursor = staticmethod(lambda: cursor)
        cursor.fetchall_rows = [
            (f"u{i}", "family", f"ev{i}", "", i, i, False, "", []) for i in range(12)
        ]

        with caplog.at_level(logging.WARNING):
            events, watermark = store.sync(calendar_id="family", since_ms=0, max_events=10)

        assert [e["uid"] for e in events] == [f"u{i}" for i in range(10)]
        assert watermark == 1000
        assert any("truncated" in r.message for r in caplog.records)

    def test_defaults_calendar_id_and_page_size(self, mock_pg):
        cal, captures, make_cursor = mock_pg
        store = _make_store(cal)
        cursor = make_cursor()
        store._conn.cursor = staticmethod(lambda: cursor)
        cursor.fetchall_rows = []

        # max_events=0 -> DEFAULT_MAX_EVENTS cap (limit = 101)
        store.sync(calendar_id="others", since_ms=7, max_events=0)
        select_params = [p for s, p in captures["executes"] if "ORDER BY updated_ms" in s]
        assert len(select_params) == 1
        assert select_params[0] == ("others", 7, cal.DEFAULT_MAX_EVENTS + 1)

    def test_participant_filter_applied(self, mock_pg):
        cal, captures, make_cursor = mock_pg
        store = _make_store(cal)
        cursor = make_cursor()
        store._conn.cursor = staticmethod(lambda: cursor)
        cursor.fetchall_rows = []

        # kid filter -> SQL gains `participants @> %s::jsonb` and the JSON-encoded
        # string param slots in between since_ms and the LIMIT (params order
        # calendar_id, since_ms, participant, max_events+1).
        store.sync(calendar_id="family", since_ms=0, max_events=10, participant="juliet")
        select_sql = [s for s, _ in captures["executes"] if "FROM cal_events" in s][0]
        assert "participants @> %s::jsonb" in select_sql
        assert "ORDER BY updated_ms" in select_sql
        select_params = [p for s, p in captures["executes"] if "FROM cal_events" in s]
        assert [list(p) for p in select_params] == [["family", 0, '"juliet"', 11]]

    def test_empty_participant_leaves_query_unchanged(self, mock_pg):
        cal, captures, make_cursor = mock_pg
        store = _make_store(cal)
        cursor = make_cursor()
        store._conn.cursor = staticmethod(lambda: cursor)
        cursor.fetchall_rows = []

        # No participant -> same exact param list as the pre-field-4 family query
        # (guards family mode: empty participant never adds the %s::jsonb clause).
        store.sync(calendar_id="family", since_ms=500, max_events=10)
        select_sql = [s for s, _ in captures["executes"] if "FROM cal_events" in s][0]
        assert "participants @> %s::jsonb" not in select_sql
        select_params = [p for s, p in captures["executes"] if "FROM cal_events" in s]
        assert [list(p) for p in select_params] == [["family", 500, 11]]


# ---------------------------------------------------------------------------
# upsert / tombstone
# ---------------------------------------------------------------------------


class TestUpsert:
    def test_upsert_issues_insert_with_jsonb_participants(self, mock_pg):
        cal, captures, _ = mock_pg
        store = _make_store(cal)
        store.upsert(
            calendar_id="family",
            uid="gcal_abc123",
            title="Swim class",
            notes="bring towel",
            start_ms=1700000000001,
            end_ms=1700000000061,
            pic="ev_abc123",
            participants=["juliet", "wesley"],
        )
        # find the INSERT...ON CONFLICT and check params (uid .. participants)
        inserts = [
            (s, p) for s, p in captures["executes"]
            if "INSERT INTO cal_events" in s and "ON CONFLICT (uid)" in s
        ]
        assert len(inserts) == 1
        sql, params = inserts[0]
        assert "participants" in sql and "%s::jsonb" in sql
        assert list(params)[:7] == [
            "gcal_abc123", "family", "Swim class", "bring towel",
            1700000000001, 1700000000061, "ev_abc123",
        ]
        assert json.loads(list(params)[7]) == ["juliet", "wesley"]

    def test_tombstone_flips_deleted_flag(self, mock_pg):
        cal, captures, _ = mock_pg
        store = _make_store(cal)
        store.tombstone("family", "gcal_abc123")
        updates = [p for s, p in captures["executes"] if "deleted = TRUE WHERE uid = %s" in s]
        assert updates == [("gcal_abc123", "family")]


# ---------------------------------------------------------------------------
# open_calendar_store
# ---------------------------------------------------------------------------


class TestOpenCalendarStore:
    def test_none_without_dsn(self, mock_pg):
        cal, _, _ = mock_pg
        cal.compose_postgres_dsn = lambda: None
        assert cal.open_calendar_store() is None

    def test_returns_store_with_dsn(self, mock_pg):
        cal, captures, _ = mock_pg
        cal.compose_postgres_dsn = lambda: "postgresql://lmao:pw@pg:5432/lmao"
        store = cal.open_calendar_store()
        assert isinstance(store, cal.CalendarStore)
        assert captures["dsn"].endswith("/lmao")

    def test_returns_none_on_connect_failure(self, mock_pg):
        cal, _, _ = mock_pg
        cal.compose_postgres_dsn = lambda: "postgresql://lmao:pw@pg:5432/lmao"
        cal.psycopg = MagicMock()
        cal.psycopg.connect.side_effect = RuntimeError("db down")
        assert cal.open_calendar_store() is None
