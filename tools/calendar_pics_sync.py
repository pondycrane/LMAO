#!/usr/bin/env python3
"""Activity picture stage for the LMAO kid calendar: web line art -> RLCD tile.

For each *upcoming* kid event (``participants`` non-empty) whose ``pic`` key is
not yet in ``cal_pics``, this stage:

1. reads the event's ACTIVITY out of its title ("J tennis 4pm" -> playing
   tennis, "W dental" -> at the dentist),
2. searches the internet for a line drawing / coloring page of a KID CARTOON
   CHARACTER doing that activity (Wikimedia Commons, plus DuckDuckGo's keyless
   image index),
3. trims the hit to its ink and fits it, aspect-preserving, into a 1-bit
   black-and-white 176x176 tile,
4. stores it under the event's ``pic`` key.

There is NO image generation model anywhere in this path, and no model call of
any kind: the queries are deterministic, the framing is a Pillow ink-bbox trim
plus a contain fit, and the quality gate is a pixel measurement.  A subject
with no acceptable online line drawing stays artless and is retried on the
next hourly run (or stored by hand with
``bazel run //tools:calendar_pics -- --key <key> --source <url>``).

Why activity art: the earlier stage painted a per-kid theme character (Pokemon
for Juliet, PAW Patrol / sea animals for Wesley) chosen at random per event, so
the panel showed an unrelated creature beside "J tennis 4pm" — and the square
stretch of a tall coloring page made even that unrecognizable.  The panel's job
is "what is happening"; the drawing now shows that.

Idempotent: a ``pic`` key already present in ``cal_pics`` is skipped, so re-runs
only do new events (``--force`` regenerates).

Usage::
    PYTHONPATH=repo python3 tools/calendar_pics_sync.py [--dry-run] [--force]
    # Backfill recent tiles (e.g. after changing the subject rule):
    PYTHONPATH=repo python3 tools/calendar_pics_sync.py --force --since-days 14
Needs the same Postgres env (LMAO_CONTACTS_URL / LMAO_CONTACTS_PG_*).
"""

from __future__ import annotations

import argparse
import io
import json
import os
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

from lma_core.calendar_pics import (  # noqa: E402
    INK_THRESHOLD,
    MAX_CONTENT_ASPECT,
    TILE_MARGIN,
    content_aspect,
    pick_tile_size,
    rasterize_line_art,
)

# ---------------------------------------------------------------------------
# Event title -> activity phrase (usable as "<person> <action> coloring page")
# ---------------------------------------------------------------------------
# Longest key first so "ice skating" is matched before "skat", and "ninja
# warrior" before "warrior".  The phrase is what the search query and the
# picture show; an event with no known activity falls back to a generic kid.
ACTIVITY_ACTIONS: dict[str, str] = {
    "ninja": "doing martial arts",
    "martial arts": "doing martial arts",
    "ice skating": "ice skating",
    "field trip": "on a field trip",
    "grandparents": "visiting grandparents",
    "grocery": "at the grocery store",
    "playdate": "playing with a friend",
    "birthday": "at a birthday party",
    "basketball": "playing basketball",
    "swimming": "swimming",
    "swim": "swimming",
    "tennis": "playing tennis",
    "soccer": "playing soccer",
    "chess": "playing chess",
    "dentist": "at the dentist",
    "dental": "at the dentist",
    "doctor": "at the doctor",
    "piano": "playing piano",
    "dance": "dancing",
    "karate": "doing karate",
    "hockey": "playing hockey",
    "surf": "surfing",
    "golf": "playing golf",
    "movie": "watching a movie",
    "park": "at the park",
    "travel": "traveling",
    "church": "at church",
    "easter": "hunting easter eggs",
    "xmas": "at christmas",
    "christmas": "at christmas",
    "test": "taking a test",
    "school": "at school",
    "princeton": "at school",
    "pingry": "at school",
    "class": "in class",
    "chinese": "in class",
    "conference": "at school",
    "assessment": "at school",
    "dismissal": "at school",
    "pick up": "at school",
    "party": "at a party",
}
DEFAULT_ACTION = ""

_UA = "LMAO-calendar-pics/1.0 (family calendar)"
# The tile size comes from lma_core.calendar_pics.pick_tile_size() at run time
# (the panel's headline slot); the ink threshold is the shared one.
_THRESHOLD = INK_THRESHOLD


def _prefer_ipv4() -> None:
    import socket as _s

    orig = _s.getaddrinfo
    _s.getaddrinfo = lambda h, p, *a, **k: sorted(
        orig(h, p, *a, **k), key=lambda r: r[0] != _s.AF_INET
    )


def extract_action(title: str) -> str:
    """The event's activity phrase, or "" when the title names none."""
    low = title.lower()
    for key in sorted(ACTIVITY_ACTIONS, key=len, reverse=True):
        if key in low:
            return ACTIVITY_ACTIONS[key]
    return DEFAULT_ACTION


def person_word(participants: list[str] | tuple[str, ...]) -> str:
    """The noun the drawing should show: one kid, or two/several kids.

    A single participant is rendered as that kid's own kind (Juliet -> girl,
    Wesley -> boy) so the drawing matches the child the panel is for; a shared
    event shows kids together.  Unknown names stay neutral.
    """
    names = {n.strip().lower() for n in participants if n and n.strip()}
    if len(names) > 1:
        return "kids"
    only = next(iter(names), "")
    if only == "juliet":
        return "girl"
    if only == "wesley":
        return "boy"
    return "kid"


def build_queries(title: str, participants: list[str] | tuple[str, ...]) -> dict:
    """Pick the event's activity and its web search queries.

    Deterministic: no model decides anything.  ``queries[0]`` is the primary
    (the kid doing the activity, as a coloring page); the rest widen the net
    without leaving the "kid cartoon character doing <activity>" intent.
    """
    action = extract_action(title)
    person = person_word(participants)
    if action:
        queries = [
            f"{person} {action} coloring page",
            f"kid {action} coloring page",
            f"{person} {action} line drawing",
        ]
    else:
        queries = [
            f"{person} cartoon coloring page",
            "happy kid cartoon line drawing",
        ]
    return {"action": action, "person": person, "queries": _dedupe(queries)}


def _dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for item in items:
        key = _norm_title(item)
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# Image sources
# ---------------------------------------------------------------------------

def _get_json(url: str, tries: int = 4) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            raise
    raise RuntimeError("image search rate-limited")


def search_commons(query: str, limit: int = 12):
    url = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(
        {
            "action": "query", "generator": "search",
            "gsrsearch": query, "gsrnamespace": 6, "gsrlimit": limit,
            "prop": "imageinfo", "iiprop": "url|mime|size", "format": "json",
        }
    )
    data = _get_json(url)
    out = []
    for p in (data.get("query", {}).get("pages", {}) or {}).values():
        ii = (p.get("imageinfo") or [{}])[0]
        if ii.get("mime", "") not in ("image/jpeg", "image/png", "image/gif"):
            continue
        out.append((p.get("title", ""), ii.get("url", ""),
                    ii.get("width", 0), ii.get("height", 0)))
    return out


# No local image generator is used or reachable from this path: tiles are
# fetched line drawings.  DuckDuckGo's image JSON API is keyless and returns
# coloring pages / line art for the kid-activity queries.
_DDG_IMAGES = "https://duckduckgo.com/i.js"
_DDG_HOME = "https://duckduckgo.com/"
_VQD_CACHE: dict[str, str] = {}
# Title/URL signals that a candidate is line art (coloring book, clipart,
# sketch) vs. a photograph.  Used by the ranker and the ink gate.
_LINE_ART_TITLE_HINTS = (
    "coloring", "colouring", "lineart", "line art", "line-art", "line drawing",
    "line-drawing", "sketch", "clipart", "clip art", "outline", "vector",
)
_PHOTO_TITLE_HINTS = ("photo", "photograph", "real life", "stock photo")
# Generated art is NOT a source: the tile must be a drawing someone drew.  An
# AI-generated vector carries these markers in its title/URL, so reject it.
_GENERATED_TITLE_HINTS = (
    "ai-generated", "ai generated", "generated vector", "generated image",
    "generative", "midjourney", "stable diffusion", "dall-e", "dalle",
)
# Multi-panel / print-page signals: a sheet of many drawings shrinks every
# figure to an unreadable speck at 176x176, so it ranks last.
_MULTI_PANEL_HINTS = (
    "pages", "gallery", "collection", "bundle", "pack", "printable", "pdf",
    "book", "set of", "vol.",
)
# The drawing must show a kid, not an object or an adult.
_KID_TITLE_HINTS = ("kid", "kids", "child", "children", "boy", "girl", "cartoon")


def _ddg_vqd(query: str) -> str:
    """Fetch the DuckDuckGo ``vqd`` token required by the image API.

    Cached per query: one hourly run searches many events, and the token is
    valid for the session, so re-fetching it per call only burns rate limit.
    """
    import re

    if query in _VQD_CACHE:
        return _VQD_CACHE[query]
    url = _DDG_HOME + "?" + urllib.parse.urlencode(
        {"q": query, "iax": "images", "ia": "images"})
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=25) as resp:
        html = resp.read().decode("utf-8", "replace")
    m = re.search(r"vqd=([0-9][0-9-]*)", html)
    if not m:
        raise RuntimeError("DuckDuckGo did not return a vqd token")
    _VQD_CACHE[query] = m.group(1)
    return m.group(1)


def search_web_images(query: str, limit: int = 20):
    """DuckDuckGo image search -> [(title, url, width, height)]."""
    vqd = _ddg_vqd(query)
    url = _DDG_IMAGES + "?" + urllib.parse.urlencode(
        {"l": "us-en", "o": "json", "q": query, "vqd": vqd, "f": ",,,", "p": "1"})
    req = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Referer": _DDG_HOME})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.load(resp)
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise
    out = []
    for r in (data.get("results") or [])[:limit]:
        if not r.get("image"):
            continue
        out.append((r.get("title", ""), r["image"],
                    int(r.get("width") or 0), int(r.get("height") or 0)))
    return out


def search_sources(query: str):
    """Internet line-drawing sources for one query.

    Wikimedia Commons carries public-domain line art; the DuckDuckGo image
    index reaches the kid-activity coloring pages Commons has no copy of.  One
    web call per query keeps the hourly run inside DuckDuckGo's rate limit;
    ranking + the ink gate then pick the usable tile.
    """
    out = list(search_commons(query))
    try:
        out += search_web_images(query)
    except Exception as exc:  # noqa: BLE001
        print(f"  (web image search unavailable for {query!r}: {exc})")
    return out


def _norm_title(t: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", t.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return "".join(c for c in t if c.isalnum())


def _score_result(r, query: str = ""):
    """Score one candidate (title,url,w,h) for *query*; None = reject."""
    title, url, w, h = r
    if not url or not w or not h:
        return None
    if url.lower().split("?")[0].endswith(".svg"):
        return None  # no SVG rasterizer in the sync image; Commons PNG/JPEG only
    if w > 6000 or h > 6000 or max(w, h) / max(1, min(w, h)) > 3.0:
        return None
    words = (query or "").split()
    subject = _norm_title(words[0]) if words else ""
    tokens = [_norm_title(t) for t in words if len(t) >= 4]
    tn = _norm_title(title)
    low = title.lower() + " " + url.lower()
    if any(h in low for h in _GENERATED_TITLE_HINTS):
        return None  # AI-generated art is not an acceptable tile source
    if tokens and not any(t in tn for t in tokens):
        return None  # no query token in the title -> unrelated, reject
    s = 0
    if subject and subject in tn:
        s += 60  # the kid noun is present
    for t in tokens:
        if t in tn and t != subject:
            s += 25  # matches the action / other query words too
    if "map" in tn or "diagram" in tn or "seating" in tn:
        s -= 20
    # Line drawings are the ONLY acceptable tile source, so a line-art signal
    # must outrank a photo of the same subject; photos fail the ink gate.
    if any(h in tn or h in url.lower() for h in _LINE_ART_TITLE_HINTS):
        s += 80
    if any(h in tn for h in _PHOTO_TITLE_HINTS):
        s -= 40
    if any(h in tn for h in _MULTI_PANEL_HINTS):
        s -= 35
    if any(h in tn for h in _KID_TITLE_HINTS):
        s += 30
    if 300 <= w <= 4000:
        s += 2
    if min(w, h) / max(1, max(w, h)) >= 0.5:
        s += 1
    return s


def rank_candidates(results, query: str = ""):
    """Return results sorted best-first that match the query (rejecting none-fit)."""
    scored = [(_score_result(r, query), r) for r in results]
    scored = [(s, r) for s, r in scored if s is not None]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored]


def pick_source(results, query: str = ""):
    """Best candidate for *query*, or None if none fit."""
    ranked = rank_candidates(results, query)
    return ranked[0] if ranked else None


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


def to_1bit_png(raw: bytes, width: int, height: int, threshold: int,
                margin: int = TILE_MARGIN) -> bytes:
    """Rasterize a line drawing to a 1-bit tile, ASPECT-PRESERVING.

    Thin wrapper over :func:`lma_core.calendar_pics.rasterize_line_art` (the
    one implementation both this stage and the by-hand tool use): trim to the
    ink, threshold at full resolution, area-average the mask down, then fit
    and center the drawing on a white tile.  The old square resize squashed a
    portrait coloring page into an unrecognizable blob.
    """
    return rasterize_line_art(raw, width, height, threshold, margin)


# A line drawing is SPARSE ink around ENCLOSED white regions.  Measured on the
# live cal_pics tiles: real line art has an ink fraction of 0.05-0.17 and
# 0.33-0.70 of the tile as white interior (the character's body).  Two failure
# modes the ink range alone cannot catch:
#   - a big white coloring page cropped badly -> almost no ink (0.027) and no
#     interior (0.000): a blank tile;
#   - a coloring page with FILLED shapes -> 0.349 ink but only 0.019 interior:
#     a solid silhouette, not line work.
# So the gate is: some ink, not mostly ink, and real enclosed white area.
LINE_ART_MIN_INK = 0.02
LINE_ART_MAX_INK = 0.45
LINE_ART_MIN_ENCLOSED = 0.15


def _bits(png_bytes: bytes) -> tuple[bytes, int, int]:
    """Decode a 1-bit PNG into (row-major MSB-first bits, width, height)."""
    with Image.open(io.BytesIO(png_bytes)) as im:
        im = im.convert("1")
        w, h = im.size
        px = im.load()
        out = bytearray((w * h + 7) // 8)
        for y in range(h):
            for x in range(w):
                if px[x, y] == 0:  # black = ink
                    idx = y * w + x
                    out[idx // 8] |= 0x80 >> (idx % 8)
        return bytes(out), w, h


def _is_ink(bits: bytes, i: int) -> bool:
    return bool(bits[i // 8] & (0x80 >> (i % 8)))


def ink_fraction(png_bytes: bytes) -> float:
    """Fraction of ink pixels in a 1-bit tile (black = ink)."""
    bits, w, h = _bits(png_bytes)
    return sum(bin(b).count("1") for b in bits) / max(1, w * h)


def enclosed_white_fraction(png_bytes: bytes) -> float:
    """Fraction of the tile that is white and NOT reachable from the border.

    That is the "inside" of a line drawing.  A filled silhouette or a blank
    page has almost none; a real outline drawing has plenty.
    """
    from collections import deque

    bits, w, h = _bits(png_bytes)
    seen = bytearray(w * h)
    queue = deque()
    for i in range(w * h):
        x, y = i % w, i // w
        if (x in (0, w - 1) or y in (0, h - 1)) and not _is_ink(bits, i):
            seen[i] = 1
            queue.append(i)
    while queue:
        i = queue.popleft()
        x, y = i % w, i // w
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < w and 0 <= ny < h:
                j = ny * w + nx
                if not seen[j] and not _is_ink(bits, j):
                    seen[j] = 1
                    queue.append(j)
    enclosed = sum(1 for i in range(w * h) if not _is_ink(bits, i) and not seen[i])
    return enclosed / max(1, w * h)


def is_line_art(png_bytes: bytes) -> bool:
    """True when the 1-bit tile is outline line work, not a photo or a blob."""
    return (
        LINE_ART_MIN_INK <= ink_fraction(png_bytes) <= LINE_ART_MAX_INK
        and enclosed_white_fraction(png_bytes) >= LINE_ART_MIN_ENCLOSED
    )


def kid_events_needing_pics(cal, pic_store, force: bool = False,
                            since_days: int = 0, limit: int = 0) -> list[tuple]:
    """Events missing a pic; ``since_days`` also includes recent (past) events.

    Default window is future events only.  ``--force --since-days N`` is the
    backfill path: every kid event that started within the last N days (or is
    still upcoming) is regenerated, so a tile picked under an old subject rule
    gets replaced.  ``limit`` caps the batch, nearest events first.
    """
    have = set(pic_store.keys()) if not force else set()
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - since_days * 86400_000
    sql = """
        SELECT uid, title, pic, participants
        FROM cal_events
        WHERE deleted = FALSE AND participants <> '[]'::jsonb AND pic <> ''
          AND start_ms > %s
        ORDER BY abs(start_ms - %s), start_ms
    """
    with cal._lock, cal._conn.cursor() as cur:
        cur.execute(sql, (cutoff_ms, now_ms))
        rows = cur.fetchall()
    needed = [r for r in rows if r[2] not in have]
    return needed[:limit] if limit > 0 else needed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="list events/keys that need a pic, do not search/store")
    parser.add_argument("--force", action="store_true",
                        help="regenerate pics even if the pic key is already stored")
    parser.add_argument("--since-days", type=int, default=0, metavar="N",
                        help="backfill window: also include events that started "
                             "within the last N days (pair with --force)")
    parser.add_argument("--limit", type=int, default=0, metavar="N",
                        help="process at most N events, nearest to now first")
    parser.add_argument("--threshold", type=int, default=_THRESHOLD)
    args = parser.parse_args(argv)

    _prefer_ipv4()

    from lma_core import calendar_pg

    cal = calendar_pg.open_calendar_store()
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

    needed = kid_events_needing_pics(cal, pic_store, force=args.force,
                                     since_days=args.since_days,
                                     limit=args.limit)
    if args.dry_run:
        print(f"=== {len(needed)} kid event(s) need a pic ===")
        for _uid, title, pic, participants in needed:
            combo = build_queries(title, participants or [])
            print(f"  {pic}  {','.join(participants):<14} {title} -> "
                  f"{combo['person']} {combo['action'] or '(no activity)'} "
                  f"queries={combo['queries']}")
        pic_store.close()
        cal.close()
        return 0

    done, failed = 0, 0
    for _uid, title, pic, participants in needed:
        combo = build_queries(title, participants or [])
        queries = combo["queries"]
        print(f"== {pic} ({','.join(participants)}): '{title}' -> "
              f"{combo['person']} {combo['action'] or '(no activity)'} "
              f"{queries} [commons + web line-art]")
        try:
            stored = False
            results, seen_urls = [], set()
            for q in queries:
                for r in search_sources(q):
                    if r[1] and r[1] not in seen_urls:
                        seen_urls.add(r[1])
                        results.append(r)
            for cand in rank_candidates(results, queries[0])[:8]:
                _, url, w, h = cand
                try:
                    raw = download(url)
                    aspect = content_aspect(raw, args.threshold)
                    if aspect is not None and not (
                            1 / MAX_CONTENT_ASPECT <= aspect <= MAX_CONTENT_ASPECT):
                        print(f"   content is a {aspect:.1f}:1 strip — skipping "
                              f"{cand[0]!r}")
                        continue
                    png = to_1bit_png(raw, tw, th, args.threshold)
                    if not is_line_art(png):
                        print(f"   not line art (ink {ink_fraction(png):.2f},"
                              f" enclosed {enclosed_white_fraction(png):.2f})"
                              f" — skipping {cand[0]!r}")
                        continue
                    rec = pic_store.put(pic, png, tw, th)
                    print(f"   stored {pic}: {cand[0]} ({w}x{h}) -> "
                          f"{rec['width']}x{rec['height']} {rec['mime']} {len(rec['bytes'])}B")
                    done += 1
                    stored = True
                    break
                except Exception as exc2:  # noqa: BLE001
                    print(f"   candidate failed ({cand[0]!r}): {exc2}")
                    continue
            if not stored:
                print(f"   no acceptable image for {queries[0]!r}")
                failed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"   FAILED for {queries[0]!r}: {exc}")
            failed += 1
        time.sleep(1.0)
    print(f"=== done: {done} stored, {failed} failed ===")
    pic_store.close()
    cal.close()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
