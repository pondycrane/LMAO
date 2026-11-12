"""Unit tests for lma_core.calendar_bundle (the ONE reply assembler).

The gRPC, framed-TCP and LXMF calendar paths all call ``build_bundle``.  The
rules it owns are the ones that had drifted between the hand-written copies:

  - a FULL fetch (since_ms == 0) drops tombstones;
  - a delta keeps them (a client needs them to remove events it holds);
  - ``pic_data`` is attached to the SINGLE headline event only;
  - weather fills ``bundle.current`` and each event's forecast;
  - the request's participant is threaded through to the store.
"""

from unittest.mock import MagicMock

import pytest

from lma_core import CalendarRequest
from lma_core.calendar_bundle import build_bundle, pick_headline_idx
from lma_core.weather import WeatherProvider

NOW_MS = 1_700_000_000_000


def _event(uid, title="Event", start_ms=NOW_MS + 60_000, end_ms=None,
           deleted=False, pic=""):
    return {
        "uid": uid, "calendar_id": "family", "title": title,
        "notes": "", "start_ms": start_ms,
        "end_ms": end_ms if end_ms is not None else start_ms + 3_600_000,
        "deleted": deleted, "pic": pic,
    }


def _store(events, watermark=1234):
    store = MagicMock()
    store.sync.return_value = (events, watermark)
    return store


def _pic_store(tile=b"\xAA" * 16):
    store = MagicMock()
    store.get.return_value = {"tile_1bpp": tile}
    return store


def _req(**kw):
    req = CalendarRequest()
    for k, v in kw.items():
        setattr(req, k, v)
    return req


class TestHeadline:
    def test_earliest_ongoing_or_future_event(self):
        events = [
            _event("past", start_ms=NOW_MS - 7_200_000, end_ms=NOW_MS - 60_000),
            _event("later", start_ms=NOW_MS + 7_200_000),
            _event("soon", start_ms=NOW_MS + 60_000),
        ]
        assert pick_headline_idx(events, NOW_MS) == 2

    def test_tombstone_is_never_the_headline(self):
        events = [_event("gone", deleted=True), _event("live")]
        assert pick_headline_idx(events, NOW_MS) == 1

    def test_none_when_nothing_visible(self):
        events = [_event("past", start_ms=NOW_MS - 7_200_000,
                         end_ms=NOW_MS - 60_000)]
        assert pick_headline_idx(events, NOW_MS) is None


class TestBuildBundle:
    def test_full_fetch_drops_tombstones_and_keeps_delta_tombstones(self):
        events = [_event("live"), _event("gone", deleted=True)]
        full = build_bundle(_store(events), _req(calendar_id="family",
                                                 since_ms=0), now_ms=NOW_MS)
        assert [e.uid for e in full.events] == ["live"]

        delta = build_bundle(_store(events), _req(calendar_id="family",
                                                  since_ms=10), now_ms=NOW_MS)
        assert [e.uid for e in delta.events] == ["live", "gone"]
        assert delta.events[1].deleted is True

    def test_full_fetch_is_the_upcoming_snapshot(self):
        """A full fetch must return what the panel can show: events that have
        not ended, ordered by START time.  The store orders by updated_ms, so a
        calendar with months of past rows used to fill the client's fixed event
        array with them and push upcoming events off the wire."""
        events = [
            _event("old-1", start_ms=NOW_MS - 90 * 86_400_000,
                   end_ms=NOW_MS - 89 * 86_400_000),
            _event("later", start_ms=NOW_MS + 7_200_000),
            _event("old-2", start_ms=NOW_MS - 30 * 86_400_000,
                   end_ms=NOW_MS - 29 * 86_400_000),
            _event("ongoing", start_ms=NOW_MS - 600_000,
                   end_ms=NOW_MS + 600_000),
            _event("soon", start_ms=NOW_MS + 60_000),
        ]
        bundle = build_bundle(_store(events), _req(since_ms=0), now_ms=NOW_MS)
        assert [e.uid for e in bundle.events] == ["ongoing", "soon", "later"]

        # A DELTA keeps the store's updated_ms ordering (the watermark
        # contract) and does not drop past events the client may still hold.
        delta = build_bundle(_store(events), _req(since_ms=10), now_ms=NOW_MS)
        assert [e.uid for e in delta.events] == [e["uid"] for e in events]

    def test_full_fetch_honours_max_events(self):
        events = [_event(f"e{i}", start_ms=NOW_MS + (i + 1) * 60_000)
                  for i in range(6)]
        bundle = build_bundle(_store(events), _req(since_ms=0, max_events=3),
                              now_ms=NOW_MS)
        assert [e.uid for e in bundle.events] == ["e0", "e1", "e2"]

    def test_pic_data_attached_to_headline_only(self):
        events = [_event("hl", pic="swim"), _event("plain", pic="school",
                                                  start_ms=NOW_MS + 7_200_000)]
        bundle = build_bundle(_store(events), _req(since_ms=0),
                              pic_store=_pic_store(), now_ms=NOW_MS)
        assert bundle.events[0].pic_data == b"\xAA" * 16
        assert bundle.events[1].pic_data == b""
        # one lookup, for the headline event only
        assert bundle.events[0].pic == "swim"

    def test_no_pic_store_leaves_pic_data_empty(self):
        bundle = build_bundle(_store([_event("hl", pic="swim")]),
                              _req(since_ms=0), now_ms=NOW_MS)
        assert bundle.events[0].pic_data == b""

    def test_weather_fills_current_and_events(self):
        provider = WeatherProvider(
            40.0, -74.0,
            fetch=lambda url, timeout: {
                "current": {"temperature_2m": 6.7, "weather_code": 0},
                "hourly": {
                    "time": [NOW_MS // 1000 // 3600 * 3600],
                    "temperature_2m": [1.9],
                    "weather_code": [71],
                },
            },
        )
        bundle = build_bundle(_store([_event("hl")]), _req(since_ms=0),
                              weather=provider.snapshot(), now_ms=NOW_MS)
        assert bundle.HasField("current")
        assert bundle.current.temp_c10 == 67
        assert bundle.events[0].weather.temp_c10 == 19
        assert bundle.events[0].weather.code == 71

    def test_no_weather_leaves_slots_absent(self):
        bundle = build_bundle(_store([_event("hl")]), _req(since_ms=0),
                              now_ms=NOW_MS)
        assert not bundle.HasField("current")
        assert not bundle.events[0].HasField("weather")

    def test_participant_threaded_to_store(self):
        store = _store([])
        build_bundle(store, _req(participant="juliet"), now_ms=NOW_MS)
        assert store.sync.call_args.kwargs["participant"] == "juliet"

    def test_defaults_resolve_calendar_and_format(self):
        bundle = build_bundle(_store([]), _req(), now_ms=NOW_MS)
        assert bundle.calendar_id == "family"
        assert bundle.format == 1
        assert bundle.watermark_ms == 1234

    def test_bad_pic_store_never_breaks_the_reply(self):
        store = MagicMock()
        store.get.side_effect = RuntimeError("db down")
        bundle = build_bundle(_store([_event("hl", pic="swim")]),
                              _req(since_ms=0), pic_store=store, now_ms=NOW_MS)
        assert bundle.events[0].uid == "hl"
        assert bundle.events[0].pic_data == b""

    def test_delta_never_serves_a_non_headline_tile(self):
        """A delta carrying only a changed NON-headline event must not serve
        that event's tile: the panel drops its cached headline drawing on a
        pic_uid mismatch, so the picture used to vanish until the next full
        fetch.  The tile must ride the TRUE headline only."""
        headline = _event("hl", pic="art-hl")
        changed = _event("later", pic="art-later",
                         start_ms=NOW_MS + 48 * 3_600_000)
        store = MagicMock()
        # first call: delta rows (since_ms=5); second: full snapshot (since=0)
        store.sync.side_effect = [([changed], 999), ([headline, changed], 999)]
        bundle = build_bundle(store, _req(since_ms=5), pic_store=_pic_store(),
                              now_ms=NOW_MS)
        served = {e.uid: len(e.pic_data) for e in bundle.events}
        assert served == {"later": 0}
        # the delta headline resolution read the full snapshot
        assert store.sync.call_args_list[1].kwargs["since_ms"] == 0

    def test_delta_serves_the_tile_when_headline_is_among_changed_rows(self):
        headline = _event("hl", pic="art-hl")
        changed = _event("later", pic="art-later",
                         start_ms=NOW_MS + 48 * 3_600_000)
        store = MagicMock()
        store.sync.side_effect = [([changed, headline], 999),
                                  ([headline, changed], 999)]
        bundle = build_bundle(store, _req(since_ms=5), pic_store=_pic_store(),
                              now_ms=NOW_MS)
        served = {e.uid: len(e.pic_data) for e in bundle.events}
        assert served == {"later": 0, "hl": 16}

    def test_delta_pic_snapshot_threads_participant(self):
        store = MagicMock()
        store.sync.side_effect = [([], 999), ([], 999)]
        build_bundle(store, _req(since_ms=5, participant="juliet"),
                     now_ms=NOW_MS)
        assert store.sync.call_args_list[1].kwargs["participant"] == "juliet"

    def test_delta_when_true_headline_already_ended_serves_no_tile(self):
        """Ended events can never be the headline: a stale-art delta must not
        resurrect a drawing for an event the panel filters out."""
        ended = _event("ended", pic="art-old",
                       start_ms=NOW_MS - 7_200_000, end_ms=NOW_MS - 60_000)
        upcoming = _event("up", pic="art-up",
                          start_ms=NOW_MS + 3_600_000)
        store = MagicMock()
        store.sync.side_effect = [([ended], 999), ([ended, upcoming], 999)]
        bundle = build_bundle(store, _req(since_ms=5), pic_store=_pic_store(),
                              now_ms=NOW_MS)
        served = {e.uid: len(e.pic_data) for e in bundle.events}
        assert served == {"ended": 0}


@pytest.mark.parametrize("since_ms,expected", [(0, ["live"]), (5, ["live", "gone"])])
def test_transport_parity_rule(since_ms, expected):
    """The same request must assemble the same event set on every transport —
    the framed-TCP copy used to skip the full-fetch tombstone filter."""
    events = [_event("live"), _event("gone", deleted=True)]
    bundle = build_bundle(_store(events), _req(since_ms=since_ms),
                          now_ms=NOW_MS)
    assert [e.uid for e in bundle.events] == expected
