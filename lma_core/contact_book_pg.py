"""Postgres-backed LMAO contact book (same public API as the SQLite one).

The receiver directory lives in a dedicated in-cluster Postgres service
(``k8s/postgres.yaml``) so it survives node-local volume churn and is readable
by any host that can reach the cluster (no cross-subnet nodePort games with
the SQLite file on the pod's local-path volume).

This module mirrors :class:`lma_core.contact_book.ContactBook` exactly —
same register/touch/is_known/find/all/state semantics, same contacts table
shape — so the server and Contacts API are backend-agnostic. psycopg is
imported lazily so the module imports on dev boxes / in tests without
Postgres.

One long-lived connection is reused (a handful of LoRa nodes, one writer).
Every operation goes through :meth:`PostgresContactBook._run`, which
reconnects once when the connection has died — a Postgres restart must not
take the message gate down with it.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
import urllib.parse
from typing import Any

from lma_core.contact_book import STATES, ContactBook, placeholder_names

try:  # psycopg 3
    import psycopg
except ImportError:  # pragma: no cover - dev/test envs without Postgres
    psycopg = None

# Connection-level failures that warrant one reconnect.  An empty tuple
# matches nothing, so the except clause stays valid when psycopg is absent.
_OPERATIONAL_ERRORS: tuple = (
    (psycopg.OperationalError,) if psycopg is not None else ()
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    delivery_hash TEXT PRIMARY KEY,
    device_name   TEXT NOT NULL UNIQUE,
    device_type   TEXT NOT NULL,
    pubkey_hex    TEXT,
    first_seen    DOUBLE PRECISION NOT NULL,
    last_seen     DOUBLE PRECISION NOT NULL,
    -- Who owns device_name: 'operator' (POST /contacts), 'device' (the name
    -- the node reports in SensorReport.node_id / its announce), or 'auto'
    -- (the <type>-<last4> placeholder).  Precedence operator > device > auto,
    -- so an operator rename is never undone by the next report.
    name_source   TEXT NOT NULL DEFAULT 'auto',
    state         TEXT NOT NULL DEFAULT 'pending',
    approved_at   DOUBLE PRECISION
)
"""

# Columns in table order, for the positional row reader below.
_COLUMNS = ("delivery_hash", "device_name", "device_type", "pubkey_hex",
            "first_seen", "last_seen", "name_source", "state", "approved_at")


def is_postgres_url(source: str) -> bool:
    return source.startswith("postgres://") or source.startswith("postgresql://")


def _row_to_dict(row: tuple) -> dict[str, Any]:
    # psycopg rows are plain tuples; read columns positionally in schema order.
    return dict(zip(_COLUMNS, row, strict=True))


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
        # A short connect timeout matters: the approval gate runs inside the
        # synchronous LXMF delivery callback, so an unreachable Postgres must
        # not stall message handling for the OS TCP timeout on every message.
        self._conn = psycopg.connect(dsn, autocommit=True, connect_timeout=5)
        with self._lock:
            self._migrate_conn()

    # ── connection / execution ──────────────────────────────────────

    def _migrate_conn(self) -> None:
        """Create or upgrade the schema in one transaction.

        Autocommit would commit each rebuild statement; a pod dying mid-rebuild
        would leave a half-migrated table, so the whole migration is wrapped.
        """
        with self._conn.transaction(), self._conn.cursor() as cur:
            self._migrate(cur)

    def _reconnect(self) -> None:
        """Replace a dead connection (and re-run the idempotent migration)."""
        with contextlib.suppress(Exception):
            self._conn.close()
        self._conn = psycopg.connect(
            self._dsn, autocommit=True, connect_timeout=5
        )
        self._migrate_conn()

    def _run(self, fn):
        """Execute *fn(cursor)* under the lock, reconnecting once on failure.

        A Postgres restart closes the pooled connection; without this the book
        (and therefore the LXMF approval gate) stays broken until the pod is
        restarted.
        """
        with self._lock:
            for attempt in (0, 1):
                try:
                    with self._conn.cursor() as cur:
                        return fn(cur)
                except _OPERATIONAL_ERRORS:
                    if attempt:
                        raise
                    self._reconnect()

    # ── schema migration ────────────────────────────────────────────

    @staticmethod
    def _primary_key(cur) -> set[str]:
        cur.execute(
            """
            SELECT a.attname
              FROM pg_index i
              JOIN pg_attribute a
                ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
             WHERE i.indrelid = 'contacts'::regclass AND i.indisprimary
            """
        )
        return {row[0] for row in cur.fetchall()}

    @staticmethod
    def _migrate(cur) -> None:
        """Create the table, or upgrade a pre-lifecycle one in place.

        Older books keyed on ``device_name`` (PK), had no ``state`` and
        carried a dead ``identity_hash`` column.  Those rows are all devices
        the old static allow-list accepted, so they migrate as ``approved``.
        """
        cur.execute("SELECT to_regclass('contacts')")
        if cur.fetchone()[0] is None:
            cur.execute(_SCHEMA)
            PostgresContactBook._drain_legacy(cur)
            return

        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'contacts'"
        )
        columns = {row[0] for row in cur.fetchall()}
        if "name_source" not in columns:
            cur.execute(
                "ALTER TABLE contacts ADD COLUMN IF NOT EXISTS "
                "name_source TEXT NOT NULL DEFAULT 'auto'"
            )
            cur.execute(
                "UPDATE contacts SET name_source = 'operator' "
                "WHERE name_source = 'auto' "
                "AND device_name NOT LIKE device_type || '-%'"
            )
            columns.add("name_source")

        needs_rebuild = (
            "state" not in columns
            or "approved_at" not in columns
            or "identity_hash" in columns
            or PostgresContactBook._primary_key(cur) != {"delivery_hash"}
        )
        if needs_rebuild:
            PostgresContactBook._rebuild(cur, columns)
        else:
            cur.execute(_SCHEMA)
        PostgresContactBook._drain_legacy(cur)

    @staticmethod
    def _drain_legacy(cur) -> None:
        """Finish a rebuild that a crash interrupted.

        The rebuild is transactional now, so this is belt-and-braces for an
        older partial run: any surviving ``contacts_legacy`` rows are merged
        (insert-only) and the scratch table is dropped.
        """
        cur.execute("SELECT to_regclass('contacts_legacy')")
        if cur.fetchone()[0] is None:
            return
        cur.execute(
            """
            INSERT INTO contacts(
                delivery_hash, device_name, device_type, pubkey_hex,
                first_seen, last_seen, name_source, state, approved_at
            )
            SELECT delivery_hash, device_name, device_type, pubkey_hex,
                   first_seen, last_seen,
                   COALESCE(name_source, 'auto'), 'approved',
                   COALESCE(approved_at, last_seen)
            FROM contacts_legacy
            ON CONFLICT DO NOTHING   -- either key; never fail startup
            """
        )
        cur.execute("DROP TABLE contacts_legacy")

    @staticmethod
    def _rebuild(cur, columns: set[str]) -> None:
        """Rebuild the table with ``delivery_hash`` as PK, preserving rows."""
        name_source = (
            "COALESCE(name_source, 'auto')"
            if "name_source" in columns
            else "CASE WHEN device_name NOT LIKE device_type || '-%' "
                 "THEN 'operator' ELSE 'auto' END"
        )
        state = "COALESCE(state, 'approved')" if "state" in columns else "'approved'"
        approved_at = (
            "COALESCE(approved_at, last_seen)" if "approved_at" in columns
            else "last_seen"
        )
        cur.execute("ALTER TABLE contacts RENAME TO contacts_legacy")
        cur.execute(_SCHEMA)
        cur.execute(
            f"""
            INSERT INTO contacts(
                delivery_hash, device_name, device_type, pubkey_hex,
                first_seen, last_seen, name_source, state, approved_at
            )
            SELECT delivery_hash, device_name, device_type, pubkey_hex,
                   first_seen, last_seen, {name_source}, {state}, {approved_at}
            FROM contacts_legacy
            """
        )
        cur.execute("DROP TABLE contacts_legacy")

    # ── writes ──────────────────────────────────────────────────────

    def register(
        self,
        delivery_hash: str,
        device_type: str,
        device_name: str | None = None,
        pubkey_hex: str | None = None,
        name_source: str = "auto",
        state: str | None = None,
    ) -> dict[str, Any]:
        """Upsert a contact (same semantics as the SQLite ContactBook)."""
        if state is not None and state not in STATES:
            raise ValueError(f"invalid contact state: {state!r}")
        delivery_hash = delivery_hash.lower()
        candidates = placeholder_names(delivery_hash, device_type)
        name = device_name or candidates[0]
        if not device_name:
            name_source = "auto"
        now = time.time()
        approved_at = now if state == "approved" else None

        def _upsert(cur, use_name, use_source):
            cur.execute(
                """
                INSERT INTO contacts(
                    delivery_hash, device_name, device_type, pubkey_hex,
                    first_seen, last_seen, name_source, state, approved_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s,
                          COALESCE(%s::text, 'pending'), %s)
                ON CONFLICT(delivery_hash) DO UPDATE SET
                    device_name = CASE
                        WHEN contacts.name_source = 'operator'
                             THEN contacts.device_name      -- operator rename wins
                        WHEN excluded.name_source = 'operator'
                             THEN excluded.device_name      -- operator (re)names it
                        WHEN contacts.name_source = 'device'
                             AND excluded.name_source = 'auto'
                             THEN contacts.device_name      -- keep the device's own name
                        ELSE excluded.device_name           -- device name / placeholder
                        END,
                    name_source = CASE
                        WHEN contacts.name_source = 'operator' THEN 'operator'
                        WHEN excluded.name_source = 'operator' THEN 'operator'
                        WHEN contacts.name_source = 'device'
                             AND excluded.name_source = 'auto' THEN 'device'
                        ELSE excluded.name_source
                        END,
                    -- The auto-learn path reports the generic 'device' type;
                    -- it must not overwrite an operator-set type.
                    device_type = CASE WHEN excluded.device_type = 'device'
                                       THEN contacts.device_type
                                       ELSE excluded.device_type
                                       END,
                    pubkey_hex = COALESCE(excluded.pubkey_hex, contacts.pubkey_hex),
                    last_seen = excluded.last_seen,
                    -- Explicit ::text: an untyped parameter in an IS NULL
                    -- comparison is IndeterminateDatatype in Postgres.
                    state = CASE WHEN %s::text IS NULL
                                 THEN contacts.state ELSE %s::text END,
                    approved_at = CASE WHEN %s::text = 'approved'
                                       THEN COALESCE(contacts.approved_at,
                                                     excluded.approved_at)
                                       ELSE contacts.approved_at
                                       END
                """,
                (delivery_hash, use_name, device_type, pubkey_hex, now, now,
                 use_source, state, approved_at, state, state, state),
            )
            cur.execute(
                "SELECT * FROM contacts WHERE delivery_hash = %s", (delivery_hash,)
            )
            return cur.fetchone()

        # Only the automatic paths may fall back to a placeholder: an operator
        # naming a device must be told the name is taken (API 409).
        attempts = [name]
        if name_source in ("device", "auto"):
            attempts += [c for c in candidates if c != name]
        for index, attempt in enumerate(attempts):
            source = name_source if attempt == name else "auto"
            try:
                row = self._run(
                    lambda cur, a=attempt, s=source: _upsert(cur, a, s)
                )
            except Exception as exc:
                if "device_name" not in str(exc) or index == len(attempts) - 1:
                    raise
                continue
            return _row_to_dict(row)

    def seed_approved(self, delivery_hash: str,
                      device_type: str = "device") -> bool:
        """Insert a cold-start ``approved`` contact; never modify an existing row.

        A placeholder name already owned by another row is disambiguated with
        the hash's leading hex, because a failed seed means a fielded device
        would be dropped by the gate.
        """
        delivery_hash = delivery_hash.lower()
        now = time.time()
        for name in placeholder_names(delivery_hash, device_type):
            try:
                return self._run(
                    lambda cur, candidate=name: self._seed(
                        cur, delivery_hash, candidate, device_type, now
                    )
                )
            except Exception as exc:
                if "device_name" not in str(exc):
                    raise
        raise RuntimeError(f"could not seed {delivery_hash}: every name taken")

    @staticmethod
    def _seed(cur, delivery_hash, name, device_type, now) -> bool:
        cur.execute(
            """
            INSERT INTO contacts(
                delivery_hash, device_name, device_type, pubkey_hex,
                first_seen, last_seen, name_source, state, approved_at
            ) VALUES (%s, %s, %s, NULL, %s, %s, 'auto', 'approved', %s)
            ON CONFLICT(delivery_hash) DO NOTHING
            """,
            (delivery_hash, name, device_type, now, now, now),
        )
        return cur.rowcount > 0

    def set_state(self, delivery_hash: str, state: str) -> bool:
        """Approve/revoke a known contact; False when the hash is unknown."""
        if state not in STATES:
            raise ValueError(f"invalid contact state: {state!r}")
        now = time.time()

        def _update(cur):
            cur.execute(
                """
                UPDATE contacts SET
                    state = %s,
                    approved_at = CASE WHEN %s = 'approved'
                                       THEN COALESCE(approved_at, %s)
                                       ELSE approved_at
                                       END
                WHERE delivery_hash = %s
                """,
                (state, state, now, delivery_hash.lower()),
            )
            return cur.rowcount > 0

        return self._run(_update)

    def touch(self, delivery_hash: str) -> bool:
        """Refresh ``last_seen`` for an active device; True if it is known."""
        def _update(cur):
            cur.execute(
                "UPDATE contacts SET last_seen = %s WHERE delivery_hash = %s",
                (time.time(), delivery_hash.lower()),
            )
            return cur.rowcount > 0

        return self._run(_update)

    # ── reads ───────────────────────────────────────────────────────

    def is_known(self, delivery_hash: str) -> bool:
        return self.state_of(delivery_hash) is not None

    def state_of(self, delivery_hash: str) -> str | None:
        def _select(cur):
            cur.execute(
                "SELECT state FROM contacts WHERE delivery_hash = %s",
                (delivery_hash.lower(),),
            )
            row = cur.fetchone()
            return row[0] if row else None

        return self._run(_select)

    def is_approved(self, delivery_hash: str) -> bool:
        return self.state_of(delivery_hash) == "approved"

    def find(
        self,
        delivery_hash: str | None = None,
        device_name: str | None = None,
        device_type: str | None = None,
        state: str | None = None,
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
        if state:
            clauses.append("state = %s")
            args.append(state)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        def _select(cur):
            cur.execute(f"SELECT * FROM contacts{where} ORDER BY device_name", args)
            return cur.fetchall()

        return [_row_to_dict(r) for r in self._run(_select)]

    def all(self, state: str | None = None) -> list[dict[str, Any]]:
        return self.find(state=state)

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

    Insert-only: a row already in Postgres is never modified, except to fill a
    missing ``pubkey_hex``.  Safe to run on every server start — an operator
    rename/retype, a revocation, and both timestamps all survive the replay of
    the legacy file (the SQLite copy is frozen at the cutover and must never
    win over live Postgres state).  Returns the number of rows copied (0 when
    the SQLite file is absent).
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

        def _copy(cur):
            count = 0
            for row in rows:
                keys = row.keys()
                name_source = (
                    row["name_source"] if "name_source" in keys
                    else ("operator" if (row["device_name"] or "")
                          and not (row["device_name"] or "").startswith(
                              (row["device_type"] or "device") + "-")
                          else "auto")
                )
                state = row["state"] if "state" in keys else "approved"
                approved_at = (
                    row["approved_at"] if "approved_at" in keys
                    else (row["last_seen"] or time.time())
                )
                cur.execute(
                    """
                    INSERT INTO contacts(
                        delivery_hash, device_name, device_type, pubkey_hex,
                        first_seen, last_seen, name_source, state, approved_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(delivery_hash) DO UPDATE SET
                        -- Fill-only: Postgres is authoritative once a row
                        -- exists.  Rewriting device_name/device_type/
                        -- name_source/first_seen/last_seen here would undo
                        -- every operator rename and rewind last_seen on each
                        -- pod start (the legacy file is a frozen snapshot).
                        pubkey_hex = COALESCE(contacts.pubkey_hex,
                                              excluded.pubkey_hex)
                    """,
                    (
                        (row["delivery_hash"] or "").lower(),
                        row["device_name"] or "",
                        row["device_type"] or "device",
                        row["pubkey_hex"],
                        row["first_seen"] or time.time(),
                        row["last_seen"] or time.time(),
                        name_source,
                        state,
                        approved_at,
                    ),
                )
                count += 1
            return count

        n = pg._run(_copy)
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
