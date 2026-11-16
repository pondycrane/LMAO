"""Tests for the Postgres-backed contact book + SQLite→PG migration path.

The PG round-trip itself is exercised by the live cluster deploy (this test
env has no Postgres); here we pin the pure pieces: URL detection, DSN
composition from k8s env parts (password via Secret), and the backfill's
no-legacy-file guard.
"""

from __future__ import annotations

import os
import sys
from unittest import mock

import pytest

from lma_core import contact_book_pg as cbp


def test_is_postgres_url():
    assert cbp.is_postgres_url("postgres://lmao:pw@postgres:5432/lmao")
    assert cbp.is_postgres_url("postgresql://lmao:pw@postgres:5432/lmao")
    assert not cbp.is_postgres_url("/tmp/contacts.db")
    assert not cbp.is_postgres_url("contacts.db")


class TestComposePostgresDsn:
    def test_none_when_no_env(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            assert cbp.compose_postgres_dsn() is None

    def test_direct_url_wins(self):
        with mock.patch.dict(
            os.environ,
            {
                "LMAO_CONTACTS_URL": "postgres://direct:1@h:5432/db",
                "LMAO_CONTACTS_PG_HOST": "other",
            },
        ):
            assert cbp.compose_postgres_dsn() == "postgres://direct:1@h:5432/db"

    def test_composes_from_parts_with_quoting(self):
        with mock.patch.dict(
            os.environ,
            {
                "LMAO_CONTACTS_PG_HOST": "postgres.default.svc.cluster.local",
                "LMAO_CONTACTS_PG_USER": "lmao",
                "LMAO_CONTACTS_PG_PASSWORD": "p@ss word/with+chars",
                "LMAO_CONTACTS_PG_DB": "lmao",
            },
        ):
            dsn = cbp.compose_postgres_dsn()
            assert dsn.startswith("postgresql://")
            assert "p%40ss+word%2Fwith%2Bchars" in dsn
            assert "postgres.default.svc.cluster.local:5432/lmao" in dsn

    def test_port_and_defaults(self):
        with mock.patch.dict(os.environ, {"LMAO_CONTACTS_PG_HOST": "postgres"}):
            assert cbp.compose_postgres_dsn() == "postgresql://lmao:@postgres:5432/lmao"


def test_backfill_noop_without_legacy_file(tmp_path):
    # No legacy SQLite file → nothing to backfill, and no PG connection is
    # attempted regardless of whether psycopg is installed.
    assert cbp.backfill_sqlite_to_pg(str(tmp_path / "nope.db"), "postgres://x") == 0


class _FakeCursor:
    """Records executed SQL and returns canned results for the migration probe."""

    def __init__(self, exists=True, columns=(), pk=("device_name",)):
        self.statements = []
        self._exists = exists
        self._columns = list(columns)
        self._pk = set(pk)

    def execute(self, sql, params=None):
        self.statements.append((" ".join(sql.split()), params))
        flat = " ".join(sql.split())
        if flat.startswith("SELECT to_regclass('contacts')"):
            self._result = [(("contacts" if self._exists else None),)]
        elif flat.startswith("SELECT to_regclass('contacts_legacy')"):
            self._result = [(None,)]
        elif flat.startswith("SELECT column_name"):
            self._result = [(c,) for c in self._columns]
        elif "indisprimary" in flat:
            self._result = [(c,) for c in self._pk]
        else:
            self._result = []

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_pg_migration_rebuilds_a_name_keyed_table():
    """A pre-lifecycle PG book (device_name PK, identity_hash column, no state)
    is rebuilt onto delivery_hash with legacy rows approved."""
    cur = _FakeCursor(
        exists=True,
        columns=("device_name", "device_type", "delivery_hash", "pubkey_hex",
                 "identity_hash", "first_seen", "last_seen", "name_source"),
    )
    cbp.PostgresContactBook._migrate(cur)
    flat = [s for s, _ in cur.statements]
    assert any(s.startswith("ALTER TABLE contacts RENAME TO contacts_legacy") for s in flat)
    insert = next(s for s in flat if s.startswith("INSERT INTO contacts("))
    assert "'approved'" in insert
    assert "identity_hash" not in insert.split("FROM contacts_legacy")[0]
    assert any(s.startswith("DROP TABLE contacts_legacy") for s in flat)


def test_pg_migration_creates_a_missing_table():
    cur = _FakeCursor(exists=False)
    cbp.PostgresContactBook._migrate(cur)
    assert cur.statements[0][0].startswith("SELECT to_regclass")
    assert cur.statements[1][0].startswith("CREATE TABLE IF NOT EXISTS contacts")
    assert "state" in cur.statements[1][0]


def test_pg_migration_is_a_noop_on_the_current_schema():
    cur = _FakeCursor(
        exists=True,
        columns=("delivery_hash", "device_name", "device_type", "pubkey_hex",
                 "first_seen", "last_seen", "name_source", "state", "approved_at"),
        pk=("delivery_hash",),
    )
    cbp.PostgresContactBook._migrate(cur)
    flat = [s for s, _ in cur.statements]
    assert not any(s.startswith("ALTER TABLE contacts RENAME") for s in flat)


def test_pg_register_types_every_parameter():
    """Postgres rejects an untyped parameter in `%s IS NULL` (indeterminate
    datatype); the state placeholders must be cast explicitly.  This bit the
    live deployment — the fake-cursor test below pins the SQL text."""
    book = object.__new__(cbp.PostgresContactBook)
    import threading

    book._lock = threading.Lock()
    book._conn = mock.MagicMock()
    statements = []

    def _run(fn):
        class _Cur:
            def execute(self, sql, params=None):
                statements.append(" ".join(sql.split()))

            def fetchone(self):
                return ("h", "n", "t", None, 0.0, 0.0, "auto", "pending", None)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return fn(_Cur())

    book._run = _run
    book.register(delivery_hash="a" * 32, device_type="cardputer",
                  state="approved")

    insert = next(s for s in statements if s.startswith("INSERT INTO contacts("))
    assert "COALESCE(%s::text, 'pending')" in insert
    assert "%s::text IS NULL" in insert
    assert "%s::text = 'approved'" in insert
    # psycopg does not strip SQL comments, so a placeholder mentioned in one
    # counts: the live deploy failed with 13 placeholders / 12 parameters.
    assert insert.count("%s") == 12


class _DeadCursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_run_reconnects_once_when_the_connection_died():
    """A Postgres restart closes the pooled connection; the book must rebuild
    it instead of failing every report until the server pod restarts."""
    book = object.__new__(cbp.PostgresContactBook)
    import threading

    book._lock = threading.Lock()
    book._dsn = "postgresql://x"
    book._conn = mock.MagicMock()
    class _ConnLost(Exception):
        pass

    book._conn.cursor.side_effect = _ConnLost("closed")
    reconnects = []

    def _reconnect():
        reconnects.append(True)
        book._conn = mock.MagicMock()

    book._reconnect = _reconnect
    calls = []

    def _fn(cur):
        calls.append(cur)
        return "ok"

    with mock.patch.object(cbp, "_OPERATIONAL_ERRORS", (_ConnLost,)):
        assert book._run(_fn) == "ok"
    assert reconnects == [True]
    assert len(calls) == 1

    # A failure that survives the reconnect propagates instead of looping.
    def _reconnect_dead():
        reconnects.append(True)
        dead = mock.MagicMock()
        dead.cursor.side_effect = _ConnLost("still down")
        book._conn = dead

    book._reconnect = _reconnect_dead
    book._conn.cursor.side_effect = _ConnLost("still down")
    with mock.patch.object(cbp, "_OPERATIONAL_ERRORS", (_ConnLost,)), \
         pytest.raises(_ConnLost):
        book._run(_fn)
    assert len(reconnects) == 2
    assert len(calls) == 1


def test_backfill_never_overwrites_live_postgres_state(tmp_path):
    """The legacy SQLite file is a frozen snapshot replayed on every start: it
    may only fill a missing pubkey, never re-apply a name, state, type or
    timestamp (that would undo an operator rename/revocation each restart)."""
    import sqlite3

    db = tmp_path / "contacts.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE contacts (delivery_hash TEXT PRIMARY KEY, "
        "device_name TEXT NOT NULL, device_type TEXT NOT NULL, pubkey_hex TEXT, "
        "first_seen REAL, last_seen REAL)"
    )
    conn.execute(
        "INSERT INTO contacts VALUES (?, 'legacy-name', 'cardputer', NULL, 1.0, 2.0)",
        ("a" * 32,),
    )
    conn.commit()
    conn.close()

    statements = []

    class _Rec:
        def execute(self, sql, params=None):
            # Drop "--" comments: they name the columns the statement must NOT
            # touch, and the assertions below match on the SQL text.
            text = " ".join(line.split("--")[0] for line in sql.splitlines())
            statements.append(" ".join(text.split()))

    fake_book = mock.MagicMock()
    fake_book._run.side_effect = lambda fn: fn(_Rec())

    with mock.patch.object(cbp, "psycopg", object()), \
         mock.patch.object(cbp, "PostgresContactBook", return_value=fake_book):
        assert cbp.backfill_sqlite_to_pg(str(db), "postgres://x") == 1

    insert, update = statements[0].split("ON CONFLICT(delivery_hash) DO UPDATE SET ")
    assert "device_name" not in update
    assert "device_type" not in update
    assert "state" not in update
    assert "last_seen" not in update
    assert "pubkey_hex" in update
    assert "COALESCE" in update


def test_pg_register_falls_back_when_the_placeholder_is_taken():
    """Two devices can share a last4; the PG path must try the longer
    placeholder instead of failing every report from the second device."""
    book = object.__new__(cbp.PostgresContactBook)
    import threading

    book._lock = threading.Lock()
    tried = []

    class _Cur:
        def execute(self, sql, params=None):
            if not " ".join(sql.split()).startswith("INSERT INTO contacts("):
                return  # the follow-up SELECT is not a new attempt
            tried.append(params[1])
            if len(tried) == 1:
                raise RuntimeError("duplicate key value: contacts_device_name_key")

        def fetchone(self):
            return ("b" * 32, tried[-1], "device", None, 0.0, 0.0, "auto",
                    "pending", None)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    book._run = lambda fn: fn(_Cur())
    entry = book.register(delivery_hash="b" * 28 + "abcd", device_type="device")

    assert tried == ["device-abcd", "device-bbbb-abcd"]
    assert entry["device_name"] == "device-bbbb-abcd"


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__] + sys.argv[1:]))
