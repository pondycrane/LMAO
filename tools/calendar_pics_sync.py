#!/usr/bin/env python3
"""Picture stage for the LMAO kid calendar: themed art -> RLCD tile in cal_pics.

For each *upcoming* kid event (``participants`` non-empty) whose ``pic`` key is
not yet in ``cal_pics``, search Wikimedia Commons for a normal image matching
the kid's theme (Juliet -> pokemon, Wesley -> sea animal), threshold it to a
1-bit black-and-white PNG at the 144x144 RLCD tile size, and store it under the
event's ``pic`` key (via the same ``PicStore`` the display reads). Idempotent:
a ``pic`` key already present is skipped, so re-runs only do new events.

Themes (per the family): Juliet -> pokemon; Wesley -> paw patrol / sea animals.
Sea-animal line/raster art is plentiful on Commons; pokemon has no public-domain
art, so on-theme Commons results (pokeball, etc.) are used as a fallback -- the
user accepted normal images thresholded to 2-color B&W rather than insisting on
copyright-clean line drawings.

Usage::
    PYTHONPATH=repo python3 tools/calendar_pics_sync.py [--dry-run]
Needs the same Postgres env (LMAO_CONTACTS_URL / LMAO_CONTACTS_PG_*).
"""

from __future__ import annotations

import argparse
import io
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    from PIL import Image  # noqa: PLC0415
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]

from lma_core.calendar_pics import PicStore, pick_tile_size  # noqa: E402

# kid nickname -> theme search terms (tried in order)
CHILD_THEMES: dict[str, list[str]] = {
    "juliet": ["pokeball", "pokemon", "cute monster", "dragon"],
    "wesley": ["dolphin", "octopus", "sea turtle", "whale", "sea animal", "fish"],
}

_UA = "LMAO-calendar-pics/1.0 (family calendar)"
_TILE_W, _TILE_H = 144, 144
_THRESHOLD = 140  # generous white so photos don't go fully black; tune per kid


def _prefer_ipv4() -> None:
    import socket as _s

    orig = _s.getaddrinfo
    _s.getaddrinfo = lambda h, p, *a, **k: sorted(
        orig(h, p, *a, **k), key=lambda r: r[0] != _s.AF_INET
    )


def _get_json(url: str, tries: int = 4) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                import json

                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            raise
    raise RuntimeError("commons search rate-limited")


def search_commons(query: str, limit: int = 12):
    """Return a list of (title, url, width, height) image hits for *query*."""
    url = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(
        {
            "action": "query", "generator": "search",
            "gsrsearch": query, "gsrnamespace": 6, "gsrlimit": limit,
            "prop": "imageinfo", "iiprop": "url|mime|size", "format": "json",
        }
    )
    data = _get_json(url)
    pages = (data.get("query", {}).get("pages", {}) or {})
    results = []
    for p in pages.values():
        ii = (p.get("imageinfo") or [{}])[0]
        mime = ii.get("mime", "")
        # normal raster images only -- svg needs cairosvg (absent); skip pdfs
        if mime not in ("image/jpeg", "image/png", "image/gif"):
            continue
        results.append((p.get("title", ""), ii.get("url", ""),
                        ii.get("width", 0), ii.get("height", 0)))
    return results


def pick_source(results, query: str = ""):
    """Pick the most usable candidate for *query*.

    Prefer results whose title actually contains a token from ``query``
    (avoids e.g. "pokeball" matching "Monster Ball Tour"), then square-ish
    raster images in a sane size range.
    """
    def norm(t: str) -> str:
        import unicodedata
        t = unicodedata.normalize("NFKD", t.lower())
        t = "".join(c for c in t if not unicodedata.combining(c))
        return "".join(c for c in t if c.isalnum())

    anchors = [norm(t) for t in (query or "").split() if len(t) >= 5]

    def score(r):
        title, url, w, h = r
        if not url or not w or not h:
            return -100
        if w > 6000 or h > 6000:  # huge maps/diagrams
            return -90
        aspect = max(w, h) / max(1, min(w, h))
        if aspect > 3.0:  # extreme panoramas / tall strips
            return -80
        tn = norm(title)
        s = 0
        if any(a in tn for a in anchors):
            s += 50  # theme token actually present -> strongly prefer
        if "map" in tn or "diagram" in tn:
            s -= 20
        if 300 <= w <= 4000:
            s += 2
        if 0.5 <= min(w, h) / max(1, max(w, h)):
            s += 1
        return s
    results = sorted(results, key=score, reverse=True)
    return results[0] if results else None


def theme_for(participants) -> str:
    """Primary theme search term from the first participant in the list."""
    for kid in participants:
        if kid in CHILD_THEMES and CHILD_THEMES[kid]:
            return CHILD_THEMES[kid][0]
    return "rabbit"


def download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            raise
    raise RuntimeError("image download rate-limited")


def to_1bit_png(raw: bytes, width: int, height: int, threshold: int) -> bytes:
    img = Image.open(io.BytesIO(raw)).convert("L")
    img = img.resize((width, height), Image.Resampling.LANCZOS)
    img = img.point(lambda v: 255 if v >= threshold else 0, mode="1")
    buf = io.BytesIO()
    img.save(buf, format="PNG", bits=1, optimize=True)
    return buf.getvalue()


def kid_events_needing_pics(cal, pic_store) -> list[tuple]:
    if pic_store is None:
        return []
    have = set(pic_store.keys())
    # upcoming kid events (participants set) whose pic isn't stored yet
    sql = """
        SELECT uid, title, pic, participants
        FROM cal_events
        WHERE deleted = FALSE AND participants <> '[]'::jsonb AND pic <> ''
          AND start_ms > (EXTRACT(epoch FROM now()) * 1000)::bigint
        ORDER BY start_ms
    """
    with cal._lock, cal._conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    return [r for r in rows if r[2] not in have]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="list events/keys that need a pic, do not search/store")
    parser.add_argument("--query", help="override the Commons search term (all keys)")
    parser.add_argument("--threshold", type=int, default=_THRESHOLD)
    args = parser.parse_args(argv)

    _prefer_ipv4()

    from lma_core import calendar_pg

    cal = calendar_pg.open_calendar_store()
    pic_store = None
    try:
        from lma_core.calendar_pics import open_pic_store  # noqa: PLC0415

        pic_store = open_pic_store()
    except Exception:
        pic_store = None

    if cal is None or pic_store is None:
        print("ERROR: no Postgres reachable (LMAO_CONTACTS_URL / LMAO_CONTACTS_PG_*)",
              file=sys.stderr)
        return 1

    tile = pick_tile_size()
    tw, th = tile[0], tile[1]

    needed = kid_events_needing_pics(cal, pic_store)
    if args.dry_run:
        print(f"=== {len(needed)} kid event(s) need a pic ===")
        for uid, title, pic, participants in needed:
            print(f"  {pic}  {','.join(participants):<14} {title}")
        pic_store.close()
        cal.close()
        return 0

    done, failed = 0, 0
    for uid, title, pic, participants in needed:
        query = args.query or theme_for(participants)
        print(f"== {pic} ({','.join(participants)}): '{title}' -> search '{query}'")
        try:
            results = search_commons(query)
            src = pick_source(results, query)
            if src is None:
                print(f"   no usable image for '{query}'")
                failed += 1
                continue
            _, url, w, h = src
            raw = download(url)
            png = to_1bit_png(raw, tw, th, args.threshold)
            rec = pic_store.put(pic, png, tw, th)
            print(f"   stored {pic}: {src[0]} ({w}x{h}) -> "
                  f"{rec['width']}x{rec['height']} {rec['mime']} {len(rec['bytes'])}B")
            done += 1
        except Exception as exc:  # noqa: BLE001
            print(f"   FAILED for '{query}': {exc}")
            failed += 1
        time.sleep(1.0)  # be gentle with Commons rate limits
    print(f"=== done: {done} stored, {failed} failed ===")
    pic_store.close()
    cal.close()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
