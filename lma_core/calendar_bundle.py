"""One calendar-reply assembler, shared by every server transport.

The family calendar is served over three paths that must produce the SAME
``CalendarBundle`` for the same request:

  - gRPC ``lma.LMAO/GetCalendar``      (RoboDog display, :50051)
  - framed TCP :4247                    (RoboDog display, plain socket)
  - LXMF ``LMAOEnvelope{ request.calendar }`` (mesh clients)

Before this module each path assembled the bundle itself, and the copies had
already drifted: the framed-TCP path lacked the full-fetch tombstone filter
and never attached ``pic_data`` (so the panel's headline drawing could not
render over TCP at all).  Assembly — including the full-fetch tombstone rule,
the single-headline picture rule and the weather attach rule — lives here
once, so a wire change lands on all three paths or none.

The caller owns the transport-specific bits: resolving the ``CalendarRequest``,
fetching the weather snapshot (blocking or async) and serializing/sending the
reply.  ``build_bundle`` is synchronous and side-effect free apart from the
``store.sync`` read and the ``pic_store.get`` read.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from lma_core import CalendarBundle
from lma_core.weather import apply_weather

_logger = logging.getLogger(__name__)

# Logical calendar when the request leaves ``calendar_id`` empty.
DEFAULT_CALENDAR_ID = "family"


def pick_headline_idx(events: list[dict[str, Any]], now_ms: int) -> int | None:
    """Index of the single event whose picture the panel shows.

    The RoboDog panel draws an image only for its first visible
    (non-tombstoned, ongoing-or-future) row, so the server attaches
    ``pic_data`` for just that one — one tile per calendar reply, never one
    per event (a 192x192 tile is 4.6 KB; sixteen of them would dwarf the
    events).  Returns the index of the earliest-starting such event in
    ``events``, or ``None`` when nothing is visible.
    """
    best: int | None = None
    for i, ev in enumerate(events):
        if ev["deleted"] or (ev.get("end_ms") or 0) < now_ms:
            continue
        if best is None or ev["start_ms"] < events[best]["start_ms"]:
            best = i
    return best


def fill_pic_data(pic_store: Any, pic_key: str, cal_entry: Any) -> bool:
    """Attach the 1-bit RLCD tile for a pic key (``CalEvent.pic_data``).

    No-op (False) when there is no key, no store, or the store has no tile.
    """
    if not pic_key or pic_store is None:
        return False
    try:
        rec = pic_store.get(pic_key)
    except Exception:  # noqa: BLE001 — a picture must never break a reply
        return False
    tile = rec.get("tile_1bpp") if rec else None
    if tile:
        cal_entry.pic_data = tile
        return True
    return False


def build_bundle(
    store: Any,
    request: Any,
    *,
    pic_store: Any = None,
    weather: Any = None,
    now_ms: int | None = None,
) -> CalendarBundle:
    """Sync ``store`` for ``request`` and assemble the reply bundle.

    ``request`` is a ``CalendarRequest`` proto (or any object exposing
    ``calendar_id``/``since_ms``/``max_events``/``participant``).

    Rules (identical for gRPC, framed TCP and LXMF):
      - ``since_ms == 0`` is a FULL fetch: it is the server's complete truth,
        so tombstones are dropped here (a delta client, by contrast, needs
        them to remove events it already holds).
      - ``pic_data`` is attached ONLY to the single headline event
        (:func:`pick_headline_idx`).
      - ``weather`` is a ``WeatherSnapshot`` (already fetched by the caller,
        or None when the deployment has no location): it fills
        ``bundle.current`` and each event's forecast.
    """
    calendar_id = request.calendar_id or DEFAULT_CALENDAR_ID
    since_ms = int(request.since_ms)
    events, watermark = store.sync(
        calendar_id=calendar_id,
        since_ms=since_ms,
        max_events=request.max_events,
        participant=request.participant or "",
    )
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    headline_uid: str | None = None
    if since_ms == 0:
        # A FULL fetch is the display's complete truth: the panel lists what
        # is ongoing or upcoming, so tombstones and events that already ended
        # are dropped, and the set is ordered by START time.  The store's own
        # order is by updated_ms (correct for delta/watermark semantics), which
        # would let a calendar with months of past rows fill the wire with them
        # and silently push upcoming events out of the client's fixed event
        # array (observed: 27 live rows, the 16 that fit were mostly past, so
        # the panel saw a single upcoming event).
        events = [e for e in events
                  if not e["deleted"] and (e.get("end_ms") or 0) >= now_ms]
        events.sort(key=lambda e: e["start_ms"])
        if request.max_events:
            events = events[: request.max_events]
        headline = pick_headline_idx(events, now_ms)
        if headline is not None:
            headline_uid = events[headline]["uid"]
    else:
        # A DELTA carries only CHANGED rows.  Picking the headline among them
        # crowns the "headline of the delta" — some unrelated edited event —
        # and serving ITS tile made the panel drop its cached headline drawing
        # (pic_uid mismatch), so the picture vanished until the next full
        # fetch.  Resolve the TRUE headline from a full snapshot instead and
        # serve its tile only when that event is itself among the delta rows;
        # otherwise serve no tile and the panel keeps its cached one.
        full_events, _ = store.sync(
            calendar_id=calendar_id,
            since_ms=0,
            max_events=0,
            participant=request.participant or "",
        )
        full_events = [e for e in full_events
                       if not e["deleted"] and (e.get("end_ms") or 0) >= now_ms]
        headline = pick_headline_idx(full_events, now_ms)
        if headline is not None:
            headline_uid = full_events[headline]["uid"]

    bundle = CalendarBundle()
    bundle.format = 1
    bundle.calendar_id = calendar_id
    for ev in events:
        entry = bundle.events.add()
        entry.uid = ev["uid"]
        entry.title = ev["title"]
        entry.notes = ev["notes"]
        entry.start_ms = ev["start_ms"]
        entry.end_ms = ev["end_ms"]
        entry.deleted = ev["deleted"]
        entry.pic = ev["pic"]
        if ev["uid"] == headline_uid:
            fill_pic_data(pic_store, ev["pic"], entry)
    bundle.watermark_ms = watermark
    if weather is not None:
        filled = apply_weather(bundle, weather)
        _logger.debug("calendar reply carries %d weather slots", filled)
    return bundle
