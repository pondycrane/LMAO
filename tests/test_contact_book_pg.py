"""Tests for the Postgres-backed contact book + SQLite→PG migration path.

The PG round-trip itself is exercised by the live cluster deploy (this test
env has no Postgres); here we pin the pure pieces: URL detection, DSN
composition from k8s env parts (password via Secret), and the backfill's
no-legacy-file guard.
"""

from __future__ import annotations

import os
from unittest import mock

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
