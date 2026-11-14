"""Postgres-backed family calendar store for LMAO (read-only on the wire).

The calendar lives in the same in-cluster Postgres that already backs the
contact book (``lmao`` db, ``k8s/postgres.yaml`` — the DSN is composed by
``contact_book_pg.compose_postgres_dsn`` so one Secret/DB/tooling serves both).
Events are edited OUT-OF-BAND by the operator with plain SQL; two triggers keep
the wire-relevant bookkeeping consistent:

- ``lmao_cal_touch_trg`` (BEFORE INSERT/UPDATE): stamps ``updated_ms`` from the
  DB clock and maintains ``rev``, so direct ``INSERT``/``UPDATE`` always stays
  syncable — the operator can omit the bookkeeping columns entirely.
- ``lmao_cal_tombstone_trg`` (BEFORE DELETE): converts a hard ``DELETE`` into a
  tombstone row (``deleted = TRUE``) and cancels the delete, so removals
  propagate to every client on the next sync.  Tombstones are kept (tiny rows;
  family-calendar volume makes a purge job unnecessary).

The LMAO protocol only READS this store: ``CalendarRequest`` (Request.kind) →
:meth:`sync` returns the delta events + a watermark the client uses as the next
``since_ms``.  Mirrors ``contact_book_pg.PostgresContactBook`` exactly — lazy
psycopg import, one shared connection with a global write lock, sync calls from
the LXMF delivery callback, no ORM/migrations.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from lma_core.contact_book_pg import compose_postgres_dsn

_logger = logging.getLogger(__name__)

try:  # psycopg 3
    import psycopg
except ImportError:  # pragma: no cover - dev/test envs without Postgres
    psycopg = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cal_events (
    uid         TEXT PRIMARY KEY,
    calendar_id TEXT NOT NULL DEFAULT 'family',
    title       TEXT NOT NULL,
    notes       TEXT NOT NULL DEFAULT '',
    start_ms    BIGINT NOT NULL,
    end_ms      BIGINT NOT NULL,
    created_ms  BIGINT NOT NULL,
    updated_ms  BIGINT NOT NULL,
    rev         INTEGER NOT NULL DEFAULT 1,
    deleted     BOOLEAN NOT NULL DEFAULT FALSE,
    pic         TEXT NOT NULL DEFAULT '',
    participants JSONB NOT NULL DEFAULT '[]',
    CHECK (end_ms >= start_ms)
);
CREATE INDEX IF NOT EXISTS cal_events_cal_updated
    ON cal_events (calendar_id, updated_ms);
"""

# For databases created before the participants column existed: idempotent
# in-place migration so the sync schema stays feeless to re-apply.
_MIGRATE_PARTICIPANTS = """
ALTER TABLE cal_events
    ADD COLUMN IF NOT EXISTS participants JSONB NOT NULL DEFAULT '[]';
"""

# Direct SQL edits keep the sync delta correct: updated_ms is stamped from the
# DB clock (same clock the sync watermark samples, so a row is never visible
# with updated_ms in the future -> never skipped by a since_ms watermark).
_TOUCH_FN = """
CREATE OR REPLACE FUNCTION lmao_cal_touch() RETURNS trigger AS $$
BEGIN
    NEW.updated_ms := (EXTRACT(epoch FROM clock_timestamp()) * 1000)::bigint;
    NEW.rev := CASE WHEN TG_OP = 'INSERT' THEN 1 ELSE OLD.rev + 1 END;
    RETURN NEW;
END $$ LANGUAGE plpgsql;
"""

_TOMBSTONE_FN = """
CREATE OR REPLACE FUNCTION lmao_cal_tombstone() RETURNS trigger AS $$
BEGIN
    UPDATE cal_events SET deleted = TRUE WHERE uid = OLD.uid AND calendar_id = OLD.calendar_id;
    RETURN NULL;  -- cancel the hard delete; the row now is a tombstone
END $$ LANGUAGE plpgsql;
"""

_TRIGGERS = """
DROP TRIGGER IF EXISTS lmao_cal_touch_trg ON cal_events;
CREATE TRIGGER lmao_cal_touch_trg
    BEFORE INSERT OR UPDATE ON cal_events
    FOR EACH ROW EXECUTE FUNCTION lmao_cal_touch();
DROP TRIGGER IF EXISTS lmao_cal_tombstone_trg ON cal_events;
CREATE TRIGGER lmao_cal_tombstone_trg
    BEFORE DELETE ON cal_events
    FOR EACH ROW EXECUTE FUNCTION lmao_cal_tombstone();
"""

# Default/ceiling page size for one sync reply.  A family calendar's sync
# window is small (events change a few times a day), so truncation is a safety
# valve, not a common path; if a reply is truncated the client should sync
# again after picking a larger max_events — the store logs when it happens.
DEFAULT_MAX_EVENTS = 100

# Google Calendar event ids: base32hex (``0-9a-v``) masters of 20-64 chars, and
# recurring instances = master + ``_YYYYMMDDTHHMMSSZ``.  The Google->Postgres
# sync is the only writer of such rows; the operator's out-of-band SQL rows use
# any other shape.  ``tombstone_missing`` prunes ONLY ids matching this, so a
# moved/re-created Google occurrence is retracted without ever being able to
# delete a hand-written row.
GCAL_UID_PATTERN = r"^[a-v0-9]{20,64}(_[0-9]{8}T[0-9]{6}Z)?$"


def _row_to_event(row: tuple) -> dict[str, Any]:
    """Map a cal_events row to the CalEvent proposal fields (schema order).

    Columns selected: uid, calendar_id, title, notes, start_ms, end_ms,
    deleted, pic, participants.
    """
    uid, calendar_id, title, notes, start_ms, end_ms, deleted, pic, participants = row
    return {
        "uid": uid,
        "calendar_id": calendar_id,
        "title": title,
        "notes": notes,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "deleted": bool(deleted),
        "pic": pic or "",
        "participants": participants or [],
    }


class CalendarStore:
    """Postgres-backed read-only family calendar.  Thread-safe."""

    def __init__(self, dsn: str) -> None:
        if psycopg is None:
            raise ImportError(
                "psycopg is not installed. Calendar store unavailable. "
                "Install with: pip install 'psycopg[binary]'"
            )
        self._lock = threading.Lock()
        self._conn = psycopg.connect(dsn, autocommit=True)
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(_SCHEMA)
                cur.execute(_MIGRATE_PARTICIPANTS)
                cur.execute(_TOUCH_FN)
                cur.execute(_TOMBSTONE_FN)
                cur.execute(_TRIGGERS)

    def sync(
        self,
        calendar_id: str,
        since_ms: int = 0,
        max_events: int = 0,
        participant: str = "",
    ) -> tuple[list[dict[str, Any]], int]:
        """Return events with ``updated_ms > since_ms`` plus a sync watermark.

        The watermark is sampled from the DB clock *before* the read and is
        safe to use as the next ``since_ms``: a row inserted concurrently with
        the read gets ``updated_ms`` at/after the watermark and is delivered on
        the next sync (worst case a duplicate, which clients upsert by uid).
        Ordering is by ``updated_ms`` for deterministic deltas.

        ``participant`` (a kid name) filters the delta to events whose JSONB
        ``participants`` array contains that kid (``participants @> '"kid"'
        ::jsonb``).  Empty string = no filter (family mode) and the SQL/
        params stay byte-identical to the unfiltered query.  The watermark
        remains the global DB clock and stays safe under the filter.

        Truncation (``max_events`` exceeded) is logged; callers/NATS should
        treat it as "ask again".
        """
        if max_events <= 0:
            max_events = DEFAULT_MAX_EVENTS
        where = "WHERE calendar_id = %s AND updated_ms > %s"
        params: list[Any] = [calendar_id, int(since_ms)]
        if participant:
            where += " AND participants @> %s::jsonb"
            params.append(json.dumps(participant))
        params.append(max_events + 1)
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT (EXTRACT(epoch FROM clock_timestamp()) * 1000)::bigint"
                )
                watermark = int(cur.fetchone()[0])
                cur.execute(
                    f"""
                    SELECT uid, calendar_id, title, notes, start_ms, end_ms, deleted, pic, participants
                    FROM cal_events
                    {where}
                    ORDER BY updated_ms
                    LIMIT %s
                    """,
                    tuple(params),
                )
                rows = cur.fetchall()
        if len(rows) > max_events:
            _logger.warning(
                "Calendar sync window exceeds %d events (truncated) — "
                "client should re-sync with a larger max_events",
                max_events,
            )
            rows = rows[:max_events]
        return [_row_to_event(r) for r in rows], watermark

    def close(self) -> None:
        with self._lock:
            self._conn.close()


    def upsert(
        self,
        calendar_id: str,
        uid: str,
        title: str,
        notes: str,
        start_ms: int,
        end_ms: int,
        pic: str,
        participants: list[str],
    ) -> None:
        """Insert or refresh an event idempotently (keyed by ``uid``).

        Used by the out-of-band Google Calendar sync tool.  ``created_ms`` is
        set on insert only; the ``lmao_cal_touch_trg`` stamp ``updated_ms`` /
        ``rev`` on every insert/update so the delta sync re-delivers changed
        rows.  ``participants`` is stored as JSONB (``["juliet","wesley"]``).
        """
        with self._lock, self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cal_events(
                    uid, calendar_id, title, notes, start_ms, end_ms, pic,
                    participants, created_ms
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s,
                    %s::jsonb,
                    (EXTRACT(epoch FROM clock_timestamp()) * 1000)::bigint
                )
                ON CONFLICT (uid) DO UPDATE SET
                    calendar_id = EXCLUDED.calendar_id,
                    title       = EXCLUDED.title,
                    notes       = EXCLUDED.notes,
                    start_ms    = EXCLUDED.start_ms,
                    end_ms      = EXCLUDED.end_ms,
                    pic         = EXCLUDED.pic,
                    participants = EXCLUDED.participants
                """,
                (uid, calendar_id, title, notes, int(start_ms), int(end_ms),
                 pic, json.dumps(participants)),
            )


    def tombstone(self, calendar_id: str, uid: str) -> None:
        """Mark an event deleted (propagates to clients as a tombstone)."""
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE cal_events SET deleted = TRUE"
                    " WHERE uid = %s AND calendar_id = %s",
                    (uid, calendar_id),
                )

    def gcal_rows_in_window(
        self, calendar_id: str, from_ms: int, to_ms: int
    ) -> list[dict[str, Any]]:
        """Sync-owned rows that START inside ``[from_ms, to_ms)``.

        "Sync-owned" = the uid has Google's id shape (``GCAL_UID_PATTERN``);
        rows the operator wrote by hand are never returned, so a dry-run prune
        report and the real prune see exactly the same set.
        """
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT uid, title, start_ms, end_ms
                    FROM cal_events
                    WHERE calendar_id = %s AND deleted = FALSE
                      AND start_ms >= %s AND start_ms < %s
                      AND uid ~ %s
                    ORDER BY start_ms
                    """,
                    (calendar_id, int(from_ms), int(to_ms), GCAL_UID_PATTERN),
                )
                rows = cur.fetchall()
        return [
            {"uid": r[0], "title": r[1], "start_ms": r[2], "end_ms": r[3]}
            for r in rows
        ]

    def tombstone_missing(
        self, calendar_id: str, keep_uids: list[str], from_ms: int, to_ms: int
    ) -> list[str]:
        """Tombstone sync-owned rows in the window that Google no longer lists.

        Google is the source of truth for the kid events, and a *moved* or
        re-created occurrence keeps its old ``uid`` row behind: the upsert only
        ever adds/refreshes ids it is given, so without this the panel shows the
        same event twice (the old instance at its old time and the new one).
        Only rows whose ``uid`` matches ``GCAL_UID_PATTERN`` are eligible, so
        operator rows survive.  ``keep_uids`` is the full set returned by the
        fetch (an empty list means "Google lists no kid events in the window"
        and everything sync-owned is retracted).

        Returns the uids actually tombstoned (the trigger stamps ``updated_ms``,
        so clients receive them as deltas on their next sync).
        """
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE cal_events SET deleted = TRUE
                    WHERE calendar_id = %s AND deleted = FALSE
                      AND start_ms >= %s AND start_ms < %s
                      AND uid ~ %s
                      AND NOT (uid = ANY(%s))
                    RETURNING uid
                    """,
                    (calendar_id, int(from_ms), int(to_ms), GCAL_UID_PATTERN,
                     list(keep_uids)),
                )
                rows = cur.fetchall()
        return [r[0] for r in rows]


def open_calendar_store() -> CalendarStore | None:
    """Open the calendar store against the shared in-cluster Postgres.

    The DSN comes from the contact-book composer (``LMAO_CONTACTS_URL`` /
    ``LMAO_CONTACTS_PG_*``) — same DB and Secret as the contact book, so no
    extra configuration is needed.  Returns ``None`` when no Postgres is
    configured (dev boxes / non-cluster runs) or when psycopg is absent.
    """
    dsn = compose_postgres_dsn()
    if not dsn:
        return None
    try:
        return CalendarStore(dsn)
    except Exception as e:
        _logger.warning("Calendar store unavailable (%s) — calendar reads off", e)
        return None
