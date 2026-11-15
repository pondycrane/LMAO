"""Central LMAO network contact book — the one device registry.

Backs the operator-facing "receiver directory" with a plain SQLite table.
The network is small (a handful of half-duplex LoRa nodes), so a single
table with a global write lock is plenty — no pooling, no migrations, no ORM.

A contact keys on :attr:`delivery_hash` (the device's ``lxmf/delivery``
destination the server addresses) and carries a human ``device_name`` plus a
``device_type`` (``cardputer``, ``sprout``, ...).  ``device_name`` is a
mutable unique attribute, not the key, so renaming one device can never
collide with another device's row.

Lifecycle (``state``) replaces the static allow-list as the live gate:

  * ``pending``  — an unknown device that reported; it is dropped and listed
    for operator approval (``POST /contacts/<hash>/approve``).
  * ``approved`` — accepted: reports flow into the pipeline, downlinks reach it.
  * ``revoked``  — explicitly denied; dropped until re-approved.

``LMAO_ALLOWED_CLIENTS`` is only a cold-start seed: at startup each listed
hash is inserted as ``approved`` if absent, and an existing row (including a
revoked one) is never touched.  The operator's approval is the trust decision.

This replaces the per-peer LMAF ``caps`` broadcast as the "who can I reach"
signal for the downlink: knowing a device is a registered contact means it is
on the LMAO network and the server can send it packets directly (issue #151).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any

STATES = ("pending", "approved", "revoked")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    delivery_hash TEXT PRIMARY KEY,
    device_name   TEXT NOT NULL UNIQUE,
    device_type   TEXT NOT NULL,
    pubkey_hex    TEXT,
    first_seen    REAL NOT NULL,
    last_seen     REAL NOT NULL,
    -- Who owns device_name: 'operator' (POST /contacts), 'device' (the name
    -- the node reports in SensorReport.node_id / its announce), or 'auto'
    -- (the <type>-<last4> placeholder).  Precedence operator > device > auto,
    -- so an operator rename is never undone by the next report.
    name_source   TEXT NOT NULL DEFAULT 'auto',
    state         TEXT NOT NULL DEFAULT 'pending',
    approved_at   REAL
)
"""

_COLUMNS = ("delivery_hash", "device_name", "device_type", "pubkey_hex",
            "first_seen", "last_seen", "name_source", "state", "approved_at")


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {name: row[name] for name in _COLUMNS}


class ContactBook:
    """SQLite-backed directory of LMAO network contacts.  Thread-safe."""

    def __init__(self, db_path: str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._migrate()
            self._conn.commit()

    # ── schema migration ────────────────────────────────────────────

    def _migrate(self) -> None:
        """Create the table, or upgrade a pre-lifecycle one in place.

        Older books keyed on ``device_name`` (PK), had no ``state`` and
        carried a dead ``identity_hash`` column.  Those rows are all devices
        the old static allow-list accepted, so they migrate as ``approved``.
        """
        columns = {
            row["name"]: row
            for row in self._conn.execute("PRAGMA table_info(contacts)")
        }
        if not columns:
            self._conn.execute(_SCHEMA)
            return

        if "name_source" not in columns:
            self._conn.execute(
                "ALTER TABLE contacts ADD COLUMN name_source TEXT NOT NULL DEFAULT 'auto'"
            )
            self._conn.execute(
                "UPDATE contacts SET name_source = 'operator' "
                "WHERE device_name NOT LIKE device_type || '-%'"
            )
            columns = {
                row["name"]: row
                for row in self._conn.execute("PRAGMA table_info(contacts)")
            }

        needs_rebuild = (
            "state" not in columns
            or "approved_at" not in columns
            or "identity_hash" in columns
            or columns["delivery_hash"]["pk"] != 1
            or columns["device_name"]["pk"] != 0
        )
        if needs_rebuild:
            self._rebuild(columns)

    def _rebuild(self, columns: dict[str, sqlite3.Row]) -> None:
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
        self._conn.execute("ALTER TABLE contacts RENAME TO contacts_legacy")
        self._conn.execute(_SCHEMA)
        self._conn.execute(
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
        self._conn.execute("DROP TABLE contacts_legacy")

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
        """Upsert a contact, keyed by ``delivery_hash``.

        ``device_name`` defaults to ``<type>-<last4 of hash>`` so a contact can
        exist and be reachable before the operator gives it a friendly name.

        ``name_source`` says who owns the name: ``'operator'`` (the contacts
        API), ``'device'`` (the name the node reports/announces) or ``'auto'``
        (the placeholder).  Precedence is operator > device > auto, so an
        operator rename survives every later report and a device's own name
        survives a placeholder refresh.

        ``state`` is only changed when explicitly given (``None`` preserves
        the existing state); a new contact defaults to ``pending``.  A
        ``device_name`` already owned by another row raises ``IntegrityError``
        rather than silently re-pointing that row (the old PK-on-name bug).
        The report path (``name_source='device'``) falls back to the
        ``<type>-<last4>`` placeholder on such a collision, so two devices
        claiming one name still get their own row.
        """
        if state is not None and state not in STATES:
            raise ValueError(f"invalid contact state: {state!r}")
        delivery_hash = delivery_hash.lower()
        placeholder = f"{device_type}-{delivery_hash[-4:]}"
        name = device_name or placeholder
        if not device_name:
            name_source = "auto"
        now = time.time()
        approved_at = now if state == "approved" else None
        with self._lock:
            try:
                self._register_locked(
                    delivery_hash, name, device_type, pubkey_hex, name_source,
                    state, now, approved_at,
                )
            except sqlite3.IntegrityError as exc:
                # Only a device's own reported name falls back: an operator
                # naming a device must be told the name is taken (API 409).
                if (name_source != "device" or "device_name" not in str(exc)
                        or name == placeholder):
                    raise
                # Another row owns that name: keep this device addressable
                # under its placeholder instead of failing every report.
                self._register_locked(
                    delivery_hash, placeholder, device_type, pubkey_hex, "auto",
                    state, now, approved_at,
                )
            self._conn.commit()
            cur = self._conn.execute(
                "SELECT * FROM contacts WHERE delivery_hash = ?", (delivery_hash,)
            )
            return _row_to_dict(cur.fetchone())

    def _register_locked(self, delivery_hash, name, device_type, pubkey_hex,
                         name_source, state, now, approved_at) -> None:
        self._conn.execute(
                """
                INSERT INTO contacts(
                    delivery_hash, device_name, device_type, pubkey_hex,
                    first_seen, last_seen, name_source, state, approved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, 'pending'), ?)
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
                    last_seen = excluded.last_seen,
                    state = CASE WHEN ? IS NULL THEN contacts.state ELSE ? END,
                    approved_at = CASE WHEN ? = 'approved'
                                       THEN COALESCE(contacts.approved_at,
                                                     excluded.approved_at)
                                       ELSE contacts.approved_at
                                       END
                """,
            (delivery_hash, name, device_type, pubkey_hex, now, now,
             name_source, state, approved_at, state, state, state),
        )

    def seed_approved(self, delivery_hash: str,
                      device_type: str = "device") -> bool:
        """Insert a cold-start ``approved`` contact; never modify an existing row.

        The env allow-list is a seed, not live config: re-seeding must not
        undo an operator's revocation or rename.  Returns True when a row was
        inserted.

        A placeholder name already owned by another row is disambiguated with
        the hash's leading hex, because a failed seed means a fielded device
        would be dropped by the gate.
        """
        delivery_hash = delivery_hash.lower()
        now = time.time()
        for name in (
            f"{device_type}-{delivery_hash[-4:]}",
            f"{device_type}-{delivery_hash[:4]}-{delivery_hash[-4:]}",
        ):
            with self._lock:
                try:
                    cur = self._conn.execute(
                        """
                        INSERT INTO contacts(
                            delivery_hash, device_name, device_type, pubkey_hex,
                            first_seen, last_seen, name_source, state, approved_at
                        ) VALUES (?, ?, ?, NULL, ?, ?, 'auto', 'approved', ?)
                        ON CONFLICT(delivery_hash) DO NOTHING
                        """,
                        (delivery_hash, name, device_type, now, now, now),
                    )
                    self._conn.commit()
                    return cur.rowcount > 0
                except sqlite3.IntegrityError as exc:
                    if "device_name" not in str(exc):
                        raise
        raise RuntimeError(f"could not seed {delivery_hash}: every name taken")

    def set_state(self, delivery_hash: str, state: str) -> bool:
        """Approve/revoke a known contact; False when the hash is unknown."""
        if state not in STATES:
            raise ValueError(f"invalid contact state: {state!r}")
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE contacts SET
                    state = ?,
                    approved_at = CASE WHEN ? = 'approved'
                                       THEN COALESCE(approved_at, ?)
                                       ELSE approved_at
                                       END
                WHERE delivery_hash = ?
                """,
                (state, state, now, delivery_hash.lower()),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def touch(self, delivery_hash: str) -> bool:
        """Refresh ``last_seen`` for an active device; True if it is known."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE contacts SET last_seen = ? WHERE delivery_hash = ?",
                (time.time(), delivery_hash.lower()),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ── reads ───────────────────────────────────────────────────────

    def is_known(self, delivery_hash: str) -> bool:
        return self.state_of(delivery_hash) is not None

    def state_of(self, delivery_hash: str) -> str | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT state FROM contacts WHERE delivery_hash = ?",
                (delivery_hash.lower(),),
            )
            row = cur.fetchone()
            return row["state"] if row else None

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
            clauses.append("delivery_hash = ?")
            args.append(delivery_hash.lower())
        if device_name:
            clauses.append("device_name = ?")
            args.append(device_name)
        if device_type:
            clauses.append("device_type = ?")
            args.append(device_type)
        if state:
            clauses.append("state = ?")
            args.append(state)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            cur = self._conn.execute(
                f"SELECT * FROM contacts{where} ORDER BY device_name", args
            )
            return [_row_to_dict(r) for r in cur.fetchall()]

    def all(self, state: str | None = None) -> list[dict[str, Any]]:
        return self.find(state=state)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
