"""Postgres-backed LMAO contact book (same public API as the SQLite one).

The receiver directory lives in a dedicated in-cluster Postgres service
(``k8s/postgres.yaml``) so it survives node-local volume churn and is readable
by any host that can reach the cluster (no cross-subnet nodePort games with
the SQLite file on the pod's local-path volume).

This module mirrors :class:`lma_core.contact_book.ContactBook` exactly —
same register/touch/is_known/find/all semantics, same contacts table shape —
so the server and Contacts API are backend-agnostic. psycopg is imported
lazily so the module imports on dev boxes / in tests without Postgres.
"""

from __future__ import annotations

import os
import threading
import time
import urllib.parse
from typing import Any

from lma_core.contact_book import ContactBook

try:  # psycopg 3
    import psycopg
except ImportError:  # pragma: no cover - dev/test envs without Postgres
    psycopg = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    device_name   TEXT PRIMARY KEY,
    device_type   TEXT NOT NULL,
    delivery_hash TEXT NOT NULL UNIQUE,
    pubkey_hex    TEXT,
    identity_hash TEXT,
    first_seen    DOUBLE PRECISION NOT NULL,
    last_seen     DOUBLE PRECISION NOT NULL
)
"""


def is_postgres_url(source: str) -> bool:
    return source.startswith("postgres://") or source.startswith("postgresql://")


def _row_to_dict(row: tuple) -> dict[str, Any]:
    # psycopg rows are plain tuples; read columns positionally in schema order.
    names = ("device_name", "device_type", "delivery_hash", "pubkey_hex",
             "identity_hash", "first_seen", "last_seen")
    return dict(zip(names, row))


class PostgresContactBook:
    """Postgres-backed directory of LMAO network contacts.  Thread-safe."""

    def __init__(self, dsn: str) -> None:
        if psycopg is None:
            raise ImportError(
                "psycopg is not installed. Postgres contact book unavailable. "
                "Install with: pip install 'psycopg[binary]'"
            )
        self._lock = threading.Lock()
        self._dsn = dsn
        self._conn = psycopg.connect(dsn, autocommit=True)
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(_SCHEMA)

    def register(
        self,
        delivery_hash: str,
        device_type: str,
        device_name: str | None = None,
        pubkey_hex: str | None = None,
        identity_hash: str | None = None,
    ) -> dict[str, Any]:
        """Upsert a contact (same semantics as the SQLite ContactBook)."""
        name = device_name or f"{device_type}-{delivery_hash[-4:]}"
        now = time.time()
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO contacts(
                        device_name, device_type, delivery_hash,
                        pubkey_hex, identity_hash, first_seen, last_seen
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(delivery_hash) DO UPDATE SET
                        device_name = CASE WHEN excluded.device_name
                                            LIKE excluded.device_type || '-%%'
                                           THEN contacts.device_name   -- auto default: keep operator-set name
                                           ELSE excluded.device_name   -- custom: honor the friendly name
                                           END,
                        device_type = excluded.device_type,
                        pubkey_hex = COALESCE(excluded.pubkey_hex, contacts.pubkey_hex),
                        identity_hash = COALESCE(excluded.identity_hash, contacts.identity_hash),
                        last_seen = excluded.last_seen
                    """,
                    (name, device_type, delivery_hash, pubkey_hex, identity_hash, now, now),
                )
                cur.execute(
                    "SELECT * FROM contacts WHERE delivery_hash = %s", (delivery_hash,)
                )
                row = cur.fetchone()
        return _row_to_dict(row)

    def touch(self, delivery_hash: str) -> bool:
        """Refresh ``last_seen`` for an active device; True if it is known."""
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE contacts SET last_seen = %s WHERE delivery_hash = %s",
                    (time.time(), delivery_hash.lower()),
                )
                return cur.rowcount > 0

    def is_known(self, delivery_hash: str) -> bool:
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM contacts WHERE delivery_hash = %s",
                    (delivery_hash.lower(),),
                )
                return cur.fetchone() is not None

    def find(
        self,
        delivery_hash: str | None = None,
        device_name: str | None = None,
        device_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return contacts matching any supplied filter (AND semantics)."""
        clauses, args = [], []
        if delivery_hash:
            clauses.append("delivery_hash = %s")
            args.append(delivery_hash.lower())
        if device_name:
            clauses.append("device_name = %s")
            args.append(device_name)
        if device_type:
            clauses.append("device_type = %s")
            args.append(device_type)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM contacts{where} ORDER BY device_name", args
                )
                rows = cur.fetchall()
        return [_row_to_dict(r) for r in rows]

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute("SELECT * FROM contacts ORDER BY device_name")
                rows = cur.fetchall()
        return [_row_to_dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def open_contact_book(source: str) -> Any:
    """Return the right contact-book backend for ``source``.

    ``postgres://`` / ``postgresql://`` → :class:`PostgresContactBook`;
    anything else is treated as a SQLite file path → :class:`ContactBook`.
    """
    if is_postgres_url(source):
        return PostgresContactBook(source)
    return ContactBook(source)


# ---------------------------------------------------------------------------
# Migration: backfill the legacy SQLite file into Postgres (idempotent).
# ---------------------------------------------------------------------------


def backfill_sqlite_to_pg(sqlite_path: str, dsn: str) -> int:
    """Copy every row from the legacy SQLite contacts file into Postgres.

    Idempotent (upsert), safe to run on every server start. Preserves the
    source timestamps and operator-set names exactly. Returns the number of
    rows backfilled (0 when the SQLite file is absent).
    """
    if psycopg is None or not os.path.isfile(sqlite_path):
        return 0
    pg = PostgresContactBook(dsn)
    n = 0
    try:
        import sqlite3 as _sqlite

        conn = _sqlite.connect(sqlite_path)
        conn.row_factory = _sqlite.Row
        rows = conn.execute("SELECT * FROM contacts").fetchall()
        with pg._conn.cursor() as cur:
            for row in rows:
                cur.execute(
                    """
                    INSERT INTO contacts(
                        device_name, device_type, delivery_hash,
                        pubkey_hex, identity_hash, first_seen, last_seen
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(delivery_hash) DO UPDATE SET
                        device_name = excluded.device_name,
                        device_type = excluded.device_type,
                        pubkey_hex = COALESCE(excluded.pubkey_hex, contacts.pubkey_hex),
                        identity_hash = COALESCE(excluded.identity_hash, contacts.identity_hash),
                        first_seen = excluded.first_seen,
                        last_seen = excluded.last_seen
                    """,
                    (
                        row["device_name"] or "",
                        row["device_type"] or "device",
                        row["delivery_hash"] or "",
                        row["pubkey_hex"],
                        row["identity_hash"],
                        row["first_seen"] or time.time(),
                        row["last_seen"] or time.time(),
                    ),
                )
                n += 1
        conn.close()
    finally:
        pg.close()
    return n


def compose_postgres_dsn() -> str | None:
    """Build a Postgres DSN from LMAO_CONTACTS_PG_* env parts, or None.

    ``LMAO_CONTACTS_URL`` wins when set; otherwise the parts are composed so
    the password can come from a Kubernetes Secret (env, not manifest text).
    """
    direct = os.environ.get("LMAO_CONTACTS_URL")
    if direct:
        return direct
    host = os.environ.get("LMAO_CONTACTS_PG_HOST")
    if not host:
        return None
    user = os.environ.get("LMAO_CONTACTS_PG_USER", "lmao")
    password = os.environ.get("LMAO_CONTACTS_PG_PASSWORD", "")
    port = os.environ.get("LMAO_CONTACTS_PG_PORT", "5432")
    db = os.environ.get("LMAO_CONTACTS_PG_DB", "lmao")
    return (
        f"postgresql://{urllib.parse.quote_plus(user)}"
        f":{urllib.parse.quote_plus(password)}@{host}:{port}/{db}"
    )
