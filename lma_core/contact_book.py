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
    last_seen     REAL NOT NULL
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
    }


class ContactBook:
    """SQLite-backed directory of LMAO network contacts.  Thread-safe."""

    def __init__(self, db_path: str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(_SCHEMA)
            self._conn.commit()

    def register(
        self,
        delivery_hash: str,
        device_type: str,
        device_name: str | None = None,
        pubkey_hex: str | None = None,
        identity_hash: str | None = None,
    ) -> dict[str, Any]:
        """Upsert a contact.

        Keyed by ``delivery_hash`` (the destination the server sends to);
        ``device_name`` defaults to ``<type>-<last4 of hash>`` so a contact can
        exist and be reachable before the operator gives it a friendly name.
        """
        name = device_name or f"{device_type}-{delivery_hash[-4:]}"
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO contacts(
                    device_name, device_type, delivery_hash,
                    pubkey_hex, identity_hash, first_seen, last_seen
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(delivery_hash) DO UPDATE SET
                    device_name = CASE WHEN excluded.device_name
                                        LIKE excluded.device_type || '-%'
                                       THEN excluded.device_name
                                       ELSE contacts.device_name END,
                    device_type = excluded.device_type,
                    pubkey_hex = COALESCE(excluded.pubkey_hex, contacts.pubkey_hex),
                    identity_hash = COALESCE(excluded.identity_hash, contacts.identity_hash),
                    last_seen = excluded.last_seen
                """,
                (name, device_type, delivery_hash, pubkey_hex, identity_hash, now, now),
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
