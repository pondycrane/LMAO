#!/usr/bin/env python3
"""Sync kid-participant Google Calendar events into the family calendar.

Reads the Google Calendar for events where a tracked child (``juliet`` /
``wesley``) is an attendee, maps each to a ``cal_events`` row (JSONB
``participants``), and upserts idempotently into the in-cluster Postgres the
LMAO server reads.

Identity / dedup
----------------
- ``uid`` = the Google event/instance id (``events.list(..., singleEvents=True)``
  expands recurring masters into per-occurrence ids, so each occurrence is its
  own row — exactly what the kids' display shows).
- Upsert is ``ON CONFLICT (uid)`` (see ``CalendarStore.upsert``), so reruns
  refresh times/participants without duplicating.
- ``pic`` is a stable per-event key (``ev_<sha1(uid)[:12]>``) so the picture
  pipeline generates/targets the same key across runs.

Child registry
--------------
Map attendee emails to kid nicknames.  Extend this dict to track more kids.
Add another theme picker in the picture stage as needed.

Usage::

    # Dry run (prints plan, writes nothing):
    PYTHONPATH=repo python3 tools/calendar_sync.py --dry-run

    # Real sync (needs LMAO_CONTACTS_URL or LMAO_CONTACTS_PG_* and a Google
    # token -- default ~/.hermes/google_token.json):
    PYTHONPATH=repo python3 tools/calendar_sync.py

Google auth token: an authorized-user token (created by Hermes'
google-workspace setup) at ``--token`` / ``LMAO_GCAL_TOKEN`` /
``~/.hermes/google_token.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from datetime import datetime, timedelta, timezone

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# ---------------------------------------------------------------- registry
# kid nickname -> attendee email
CHILDREN: dict[str, str] = {
    "juliet": "ecodinquaqua@gmail.com",
    "wesley": "pondycrane@gmail.com",
}

# Title-heuristic fallback: until the user adds kids as attendees on every
# event, also tag an event from its summary.  A kid is recognised by their
# spelled-out name or by their title initial (J = Juliet, W = Wesley).
_NAME_KIDS: dict[tuple[str, ...], str] = {
    ("juliet", "juliette"): "juliet",
    ("wesley",): "wesley",
}
_INITIAL_KIDS: dict[str, str] = {
    "J": "juliet",
    "W": "wesley",
}

def _prefer_ipv4() -> None:
    """Prefer IPv4 answers when resolving API hosts.

    This Pi has no IPv6 default route; ``getaddrinfo`` returns AAAA records
    first and some HTTP clients fail with ENETUNREACH instead of falling back
    to IPv4.  Reordering so AF_INET comes first keeps the Google fetch working
    here (and is harmless on dual-stack hosts).
    """
    import socket

    orig = socket.getaddrinfo

    def _shim(host, port, *args, **kwargs):
        results = orig(host, port, *args, **kwargs)
        return sorted(results, key=lambda r: r[0] != socket.AF_INET)

    socket.getaddrinfo = _shim


_CALENDAR_ID = "family"
_TZ = "America/New_York"


def _kids_from_title(title: str) -> list[str]:
    """Kids identifiable from an event summary (name or initial token)."""
    low = title.lower()
    kids: set[str] = set()
    for tokens, nick in _NAME_KIDS.items():
        if any(tok in low for tok in tokens):
            kids.add(nick)
    for token in title.split():
        if token in _INITIAL_KIDS:
            kids.add(_INITIAL_KIDS[token])
    return sorted(kids)


def build_pic_key(uid: str) -> str:
    """Stable per-event picture key: ev_<sha1(uid)[:12]>."""
    return "ev_" + hashlib.sha1(uid.encode()).hexdigest()[:12]


def _to_epoch_ms(value: str, is_date_only: bool, tz_name: str) -> int:
    """Convert a Google ``dateTime``/``date`` to epoch milliseconds.

    ``dateTime`` carries an offset already; ``date`` (all-day) has none, so we
    assume the family timezone at local midnight.
    """
    if not is_date_only:
        # value like 2026-10-17T17:00:00-04:00
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    from zoneinfo import ZoneInfo

    d = datetime.fromisoformat(value)  # YYYY-MM-DD
    midnight = datetime(
        d.year, d.month, d.day, tzinfo=ZoneInfo(tz_name)
    )
    return int(midnight.timestamp() * 1000)


def _participants_for(attendee_emails: list[str]) -> list[str]:
    kids = [
        nick for nick, email in CHILDREN.items()
        if email.lower() in {a.lower() for a in attendee_emails}
    ]
    # deterministic ordering for stable EQ across reruns
    return sorted(kids)


def build_event(event: dict, tz_name: str = _TZ) -> dict | None:
    """Map a Google event dict to a cal_events row (or None if no child is on it).

    A kid is "on" the event if they are (a) an attendee by email or (b)
    identified by the title fallback (name or initial).
    """
    attendees = [a.get("email", "") for a in event.get("attendees", [])]
    participants = sorted(
        set(_participants_for(attendees)) | set(_kids_from_title(event.get("summary", "")))
    )
    if not participants:
        return None

    uid = event.get("id", "")
    if not uid:
        return None

    start = event.get("start", {}) or {}
    end = event.get("end", {}) or {}
    start_date = start.get("date")
    start_dt = start.get("dateTime")
    end_date = end.get("date")
    end_dt = end.get("dateTime")

    if start_dt:
        start_ms = _to_epoch_ms(start_dt, False, tz_name)
        end_ms = _to_epoch_ms(end_dt or start_dt, False, tz_name)
    elif start_date:
        start_ms = _to_epoch_ms(start_date, True, tz_name)
        end_ms = _to_epoch_ms(end_date or start_date, True, tz_name) + 1
    else:
        start_ms = end_ms = 0

    if end_ms < start_ms:
        end_ms = start_ms

    return {
        "uid": uid,
        "calendar_id": _CALENDAR_ID,
        "title": event.get("summary", "") or "(untitled)",
        "notes": event.get("description", "") or "",
        "start_ms": start_ms,
        "end_ms": end_ms,
        "pic": build_pic_key(uid),
        "participants": participants,
    }


def fetch_kid_events(token_path: str, window_days: int):
    """Return upcoming Google event dicts (singleEvents) in the window.

    Fetches all events (a family calendar is small) and lets ``build_event``
    decide which are a tracked kid's — so both attendee-matched and
    title-heuristic-matched events are considered.  Future-only (no backfill):
    ``timeMin`` is now.
    """
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(token_path)
    service = build("calendar", "v3", credentials=creds)

    now = datetime.now(timezone.utc)
    tmin = now.isoformat()
    tmax = (now + timedelta(days=window_days)).isoformat()

    out = []
    page_token = None
    while True:
        res = service.events().list(
            calendarId="primary", timeMin=tmin, timeMax=tmax,
            maxResults=250, singleEvents=True, orderBy="startTime",
            pageToken=page_token,
        ).execute()
        out.extend(res.get("items", []))
        page_token = res.get("nextPageToken")
        if not page_token:
            break
    return out


def sync(store, events: list[dict], dry_run: bool) -> list[dict]:
    """Upsert mapped events; returns the rows that would be written."""
    planned = []
    for ev in events:
        row = build_event(ev)
        if row is None:
            continue
        planned.append(row)
        if not dry_run:
            store.upsert(
                calendar_id=row["calendar_id"], uid=row["uid"],
                title=row["title"], notes=row["notes"],
                start_ms=row["start_ms"], end_ms=row["end_ms"],
                pic=row["pic"], participants=row["participants"],
            )
    return planned


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="query + print the plan, write nothing")
    parser.add_argument("--window-days", type=int, default=14,
                        help="days ahead to scan (default 14)")
    parser.add_argument("--token", default=os.environ.get(
        "LMAO_GCAL_TOKEN", os.path.expanduser("~/.hermes/google_token.json")),
        help="Google authorized-user token path")
    args = parser.parse_args(argv)

    _prefer_ipv4()  # host quirk: no IPv6 route on this Pi

    try:
        events = fetch_kid_events(args.token, args.window_days)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: Google fetch failed: {exc}", file=sys.stderr)
        return 1

    from lma_core import calendar_pg

    store = calendar_pg.open_calendar_store()
    if store is None and not args.dry_run:
        print("ERROR: no Postgres reachable (LMAO_CONTACTS_URL / LMAO_CONTACTS_PG_*)",
              file=sys.stderr)
        return 1

    fmt = "PLAN" if args.dry_run else "UPSERT"
    print(f"=== {fmt}: {len(events)} matching Google event(s) in next {args.window_days}d ===")
    planned = []
    for ev in events:
        row = build_event(ev)
        if row is None:
            continue
        planned.append(row)
        from datetime import datetime as _dt

        st = _dt.fromtimestamp(row["start_ms"] / 1000, tz=timezone.utc).isoformat()
        print(
            f"  {row['uid']:<34} {st}  {','.join(row['participants']):<14} "
            f"pic={row['pic']}  {row['title']}"
        )

    if not args.dry_run and store is not None:
        sync(store, events, dry_run=False)
        store.close()
        print(f"=== wrote {len(planned)} row(s) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
