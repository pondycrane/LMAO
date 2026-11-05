"""Central LMAO network contact book.

Backs the operator-facing "receiver directory" with a plain SQLite table.
The network is small (a handful of half-duplex LoRa nodes), so a single
table with a global write lock is plenty — no pooling, no migrations, no ORM.

A contact keys on :attr:`delivery_hash` (the device's ``lxmf/delivery``
destination the server addresses) and carries a human ``device_name`` plus a
``device_type`` (``cardputer``, ``sprout``, ...).  The server auto-learns a
contact the first time a device reports (so it can be reached even before it
is named), and the operator can rename/retype it via :meth:`register`.

This replaces the per-peer LMAF ``caps`` broadcast as the "who can I reach"
signal for the downlink: knowing a device is a registered contact means it is
on the LMAO network and the server can send it packets directly (issue #151).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    device_name   TEXT PRIMARY KEY,
    device_type   TEXT NOT NULL,
    delivery_hash TEXT NOT NULL UNIQUE,
    pubkey_hex    TEXT,
    identity_hash TEXT,
    first_seen    REAL NOT NULL,
    last_seen     REAL NOT NULL,
    -- Who owns device_name: 'operator' (POST /contacts), 'device' (the name
    -- the node reports in SensorReport.node_id / its announce), or 'auto'
    -- (the <type>-<last4> placeholder).  Precedence operator > device > auto,
    -- so an operator rename is never undone by the next report.
    name_source   TEXT NOT NULL DEFAULT 'auto'
)
"""


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "device_name": row["device_name"],
        "device_type": row["device_type"],
        "delivery_hash": row["delivery_hash"],
        "pubkey_hex": row["pubkey_hex"],
        "identity_hash": row["identity_hash"],
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
        "name_source": row["name_source"],
    }


class ContactBook:
    """SQLite-backed directory of LMAO network contacts.  Thread-safe."""

    def __init__(self, db_path: str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(_SCHEMA)
            self._migrate_name_source()
            self._conn.commit()

    def _migrate_name_source(self) -> None:
        """Add ``name_source`` to a contacts table created before it existed.

        A pre-existing custom name (anything that is not the auto
        ``<type>-<last4>`` placeholder) is treated as operator-owned, so the
        learn path cannot rename a device that was already named by hand.
        """
        columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(contacts)")
        }
        if "name_source" in columns:
            return
        self._conn.execute(
            "ALTER TABLE contacts ADD COLUMN name_source TEXT NOT NULL DEFAULT 'auto'"
        )
        self._conn.execute(
            "UPDATE contacts SET name_source = 'operator' "
            "WHERE device_name NOT LIKE device_type || '-%'"
        )

    def register(
        self,
        delivery_hash: str,
        device_type: str,
        device_name: str | None = None,
        pubkey_hex: str | None = None,
        identity_hash: str | None = None,
        name_source: str = "auto",
    ) -> dict[str, Any]:
        """Upsert a contact.

        Keyed by ``delivery_hash`` (the destination the server sends to);
        ``device_name`` defaults to ``<type>-<last4 of hash>`` so a contact can
        exist and be reachable before the operator gives it a friendly name.

        ``name_source`` says who owns the name: ``'operator'`` (the contacts
        API), ``'device'`` (the name the node reports/announces) or ``'auto'``
        (the placeholder).  Precedence is operator > device > auto, so an
        operator rename survives every later report and a device's own name
        survives a placeholder refresh.
        """
        name = device_name or f"{device_type}-{delivery_hash[-4:]}"
        if not device_name:
            name_source = "auto"
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO contacts(
                    device_name, device_type, delivery_hash,
                    pubkey_hex, identity_hash, first_seen, last_seen,
                    name_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(delivery_hash) DO UPDATE SET
                    device_name = CASE
                        WHEN contacts.name_source = 'operator'
                             THEN contacts.device_name          -- operator rename wins
                        WHEN excluded.name_source = 'operator'
                             THEN excluded.device_name          -- operator (re)names it
                        WHEN contacts.name_source = 'device'
                             AND excluded.name_source = 'auto'
                             THEN contacts.device_name          -- keep the device's own name
                        ELSE excluded.device_name               -- device name / placeholder
                        END,
                    name_source = CASE
                        WHEN contacts.name_source = 'operator' THEN 'operator'
                        WHEN excluded.name_source = 'operator' THEN 'operator'
                        WHEN contacts.name_source = 'device'
                             AND excluded.name_source = 'auto' THEN 'device'
                        ELSE excluded.name_source
                        END,
                    -- The auto-learn path always reports the generic 'device'
                    -- type; that must not overwrite an operator-set type
                    -- (POST :8081/contacts {type:...}).
                    device_type = CASE WHEN excluded.device_type = 'device'
                                       THEN contacts.device_type
                                       ELSE excluded.device_type
                                       END,
                    pubkey_hex = COALESCE(excluded.pubkey_hex, contacts.pubkey_hex),
                    identity_hash = COALESCE(excluded.identity_hash, contacts.identity_hash),
                    last_seen = excluded.last_seen
                """,
                (name, device_type, delivery_hash, pubkey_hex, identity_hash,
                 now, now, name_source),
            )
            self._conn.commit()
            cur = self._conn.execute(
                "SELECT * FROM contacts WHERE delivery_hash = ?", (delivery_hash,)
            )
            return _row_to_dict(cur.fetchone())

    def touch(self, delivery_hash: str) -> bool:
        """Refresh ``last_seen`` for an active device; True if it is known."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE contacts SET last_seen = ? WHERE delivery_hash = ?",
                (time.time(), delivery_hash.lower()),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def is_known(self, delivery_hash: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM contacts WHERE delivery_hash = ?", (delivery_hash.lower(),)
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
            clauses.append("delivery_hash = ?")
            args.append(delivery_hash.lower())
        if device_name:
            clauses.append("device_name = ?")
            args.append(device_name)
        if device_type:
            clauses.append("device_type = ?")
            args.append(device_type)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            cur = self._conn.execute(
                f"SELECT * FROM contacts{where} ORDER BY device_name", args
            )
            return [_row_to_dict(r) for r in cur.fetchall()]

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM contacts ORDER BY device_name")
            return [_row_to_dict(r) for r in cur.fetchall()]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
