"""Unit tests for tools/calendar_sync.py mapping (no network / no DB)."""

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from tools import calendar_sync as cs  # noqa: E402


def _ev(uid="abc123", attendees=("juliet",), summary="Swim class",
        start="2026-10-17T17:00:00-04:00", end="2026-10-17T18:00:00-04:00",
        date=None, description=""):
    s = {"dateTime": start} if date is None else {"date": date}
    e = {"dateTime": end} if date is None else {"date": date}
    return {
        "id": uid,
        "summary": summary,
        "description": description,
        "attendees": [{"email": cs.CHILDREN[a]} for a in attendees],
        "start": s,
        "end": e,
    }


class TestParticipants:
    def test_maps_emails_to_kids(self):
        assert cs._participants_for(
            ["ecodinquaqua@gmail.com", "someone@x.com"]
        ) == ["juliet"]
        assert cs._participants_for(
            ["pondycrane@gmail.com", "ecodinquaqua@gmail.com"]
        ) == ["juliet", "wesley"]
        assert cs._participants_for(["someone@x.com"]) == []

    def test_case_insensitive(self):
        assert cs._participants_for(["ECODINQUAQUA@GMAIL.COM"]) == ["juliet"]


class TestBuildEvent:
    def test_skips_event_without_tracked_child(self):
        ev = _ev(attendees=())
        assert cs.build_event(ev) is None

    def test_maps_row_fields(self):
        row = cs.build_event(_ev())
        assert row["uid"] == "abc123"
        assert row["calendar_id"] == "family"
        assert row["title"] == "Swim class"
        assert row["participants"] == ["juliet"]
        # 2026-10-17T17:00:00-04:00 (datetime carries the offset)
        from datetime import datetime as _dt
        assert row["start_ms"] == int(_dt.fromisoformat("2026-10-17T17:00:00-04:00").timestamp() * 1000)
        assert row["end_ms"] == int(_dt.fromisoformat("2026-10-17T18:00:00-04:00").timestamp() * 1000)
        assert row["pic"].startswith("ev_")
        assert row["pic"] == cs.build_pic_key("abc123")

    def test_all_day_event_uses_local_midnight(self):
        row = cs.build_event(_ev(date="2026-04-10"))
        # 2026-04-10 00:00 America/New_York (EDT)
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        expected = int(_dt(2026, 4, 10, tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000)
        assert row["start_ms"] == expected
        assert row["end_ms"] > row["start_ms"]

    def test_both_kids_stamped(self):
        row = cs.build_event(_ev(attendees=("juliet", "wesley")))
        assert row["participants"] == ["juliet", "wesley"]

    def test_title_name_without_attendee(self):
        # internet-style recurring title, no guest invite
        row = cs.build_event(_ev(attendees=(), summary="Juliet dentist"))
        assert row["participants"] == ["juliet"]

    def test_title_initial_matches_kid(self):
        assert cs.build_event(_ev(attendees=(), summary="W ice skating"))["participants"] == ["wesley"]
        assert cs.build_event(_ev(attendees=(), summary="J tennis"))["participants"] == ["juliet"]

    def test_title_with_no_kid_is_none(self):
        assert cs.build_event(_ev(attendees=(), summary="Parent team lunch")) is None

    def test_attendee_plus_title_unions(self):
        row = cs.build_event(_ev(attendees=("juliet",), summary="W and Juliet piano"))
        assert row["participants"] == ["juliet", "wesley"]


class TestPicKey:
    def test_stable_and_unique(self):
        assert cs.build_pic_key("a") == cs.build_pic_key("a")
        assert cs.build_pic_key("a") != cs.build_pic_key("b")
        assert len(cs.build_pic_key("a")) == 3 + 12


class _FakeStore:
    """Records upserts / prunes; no Postgres."""

    def __init__(self, existing=()):
        self.upserts = []
        self.pruned_calls = []
        self.existing = list(existing)

    def upsert(self, **kw):
        self.upserts.append(kw)

    def gcal_rows_in_window(self, calendar_id, from_ms, to_ms):
        return self.existing

    def tombstone_missing(self, calendar_id, keep_uids, from_ms, to_ms):
        keep = set(keep_uids)
        hit = [r["uid"] for r in self.existing if r["uid"] not in keep]
        self.pruned_calls.append((calendar_id, list(keep_uids), from_ms, to_ms))
        return hit


class TestRetraction:
    """A moved/re-created Google occurrence must not leave a ghost row.

    Regression: the chess occurrence was moved 214500Z -> 223000Z and both
    rows stayed live, so the panel listed the same event twice.
    """

    def test_moved_occurrence_is_retracted(self):
        store = _FakeStore(existing=[
            {"uid": "moved_old", "title": "J new Chess 4:45"},
            {"uid": "moved_new", "title": "J new Chess 5:45pm"},
        ])
        events = [_ev(uid="moved_new")]
        _, pruned = cs.sync(store, events, dry_run=False, now_ms=1_000,
                            window_days=14)
        assert [r["uid"] for r in pruned] == ["moved_old"]
        assert [u["uid"] for u in store.upserts] == ["moved_new"]
        # the keep-set is every id Google still lists, not just kid-mapped ones
        assert store.pruned_calls == [("family", ["moved_new"], 1_000,
                                       1_000 + 14 * 86_400_000)]

    def test_unrelated_events_are_kept(self):
        store = _FakeStore(existing=[{"uid": "keep", "title": "Swim"}])
        _, pruned = cs.sync(store, [_ev(uid="keep")], dry_run=False,
                            now_ms=0, window_days=14)
        assert pruned == []

    def test_empty_fetch_retracts_everything_sync_owned(self):
        # Google lists nothing in the window -> every sync-owned row is stale.
        store = _FakeStore(existing=[{"uid": "a"}, {"uid": "b"}])
        _, pruned = cs.sync(store, [], dry_run=False, now_ms=5,
                            window_days=7)
        assert sorted(r["uid"] for r in pruned) == ["a", "b"]

    def test_dry_run_writes_nothing_but_reports_the_prune(self):
        store = _FakeStore(existing=[{"uid": "ghost"}])
        planned, pruned = cs.sync(store, [_ev(uid="live")], dry_run=True,
                                  now_ms=0, window_days=14)
        assert store.upserts == []
        assert store.pruned_calls == []
        assert [r["uid"] for r in planned] == ["live"]
        assert [r["uid"] for r in pruned] == ["ghost"]
