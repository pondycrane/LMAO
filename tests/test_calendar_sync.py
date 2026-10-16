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
