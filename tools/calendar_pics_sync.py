#!/usr/bin/env python3
"""Picture stage for the LMAO kid calendar: themed art -> RLCD tile in cal_pics.

For each *upcoming* kid event (``participants`` non-empty) whose ``pic`` key is
not yet in ``cal_pics``, this stage:

1. reasons about the best SEARCH QUERIES for that event (seeded per event),
2. searches one or more image sources (Wikimedia Commons, optional Google),
3. thresholds the best normal image to a 1-bit black-and-white 144x144 tile,
4. stores it under the event's ``pic`` key.

Subjects are paired with the event's ACTION and are kept physically coherent
(a legless dolphin is not asked to ice-skate; Wesley's sea-animal theme falls
back to a limbed sea animal like a seal/otter/penguin, Juliet's pokemon theme
can play any sport).  The subject is chosen pseudo-randomly per event (deterministic
per ``uid`` so re-runs are idempotent) rather than always the same animal.

Model reasoning: if ``LMAO_IMAGE_LLM_URL`` points at an OpenAI-compatible chat
endpoint, a small prompt asks it to return 2-3 image-search queries pairing the
theme subject with the event action; otherwise a deterministic reasoned fallback
builds them.

Idempotent: a ``pic`` key already present in ``cal_pics`` is skipped, so re-runs
only do new events (``--force`` regenerates).

Usage::
    PYTHONPATH=repo python3 tools/calendar_pics_sync.py [--dry-run] [--force]
Needs the same Postgres env (LMAO_CONTACTS_URL / LMAO_CONTACTS_PG_*).
Google Images source requires GOOGLE_CSE_KEY + GOOGLE_CSE_CX (Custom Search API).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import random
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

# ---------------------------------------------------------------------------
# Theme subject pools
# ---------------------------------------------------------------------------
# Juliet -> pokemon-ish (cartoon can do any action). Wesley -> sea animals.
POKE_SUBJECTS = [
    "pikachu", "squirtle", "bulbasaur", "charmander", "psyduck", "jigglypuff",
    "snorlax", "poliwag", "goldeen", "slowpoke", "pokeball",
]
SEA_SUBJECTS = [
    "dolphin", "orca", "sea turtle", "octopus", "crab", "jellyfish",
    "whale", "shark", "lobster", "seal", "sea lion", "otter", "penguin",
]
# for Wesley, a "limbed/coherent" pool per action (no legless fish doing tennis)
SEA_COHERENT: dict[str, list[str]] = {
    "ice skating":   ["seal", "sea lion", "otter", "penguin"],
    "tennis":        ["octopus", "seal", "sea turtle", "crab", "sea lion"],
    "soccer":        ["octopus", "seal", "sea turtle", "crab", "sea lion"],
    "basketball":    ["octopus", "seal", "crab", "sea lion"],
    "hockey":        ["seal", "sea lion", "otter", "penguin"],
    "karate":        ["octopus", "seal", "crab", "sea turtle"],
    "dance":         ["seal", "octopus", "sea lion", "sea turtle"],
    "swimming":      SEA_SUBJECTS,   # everyone swims
    "surfing":       SEA_SUBJECTS,
}
DEFAULT_ACTION = ""

# event-title keyword -> action phrase used in the search query
ACTIVITY_ACTIONS: dict[str, str] = {
    "tennis": "tennis", "skat": "ice skating", "swim": "swimming",
    "soccer": "soccer", "chess": "chess", "dentist": "dentist",
    "doctor": "doctor", "birthday": "birthday party", "party": "party",
    "piano": "piano", "dance": "dance", "karate": "karate",
    "school": "school", "golf": "golf", "hockey": "hockey",
    "surf": "surfing", "basketball": "basketball",
}

_UA = "LMAO-calendar-pics/1.0 (family calendar)"
_TILE_W, _TILE_H = 144, 144
_THRESHOLD = 140  # generous white so photos don't go fully black


def _prefer_ipv4() -> None:
    import socket as _s

    orig = _s.getaddrinfo
    _s.getaddrinfo = lambda h, p, *a, **k: sorted(
        orig(h, p, *a, **k), key=lambda r: r[0] != _s.AF_INET
    )


def _seeded_choice(pool: list[str], uid: str) -> str:
    rng = random.Random(hashlib.sha1(uid.encode()).hexdigest())
    return rng.choice(pool)


def extract_action(title: str) -> str:
    low = title.lower()
    for key, action in ACTIVITY_ACTIONS.items():
        if key in low:
            return action
    return DEFAULT_ACTION


def pick_subject(participant: str, action: str, uid: str) -> str:
    """A coherent, seeded per-event subject for the participant's theme."""
    if participant == "juliet":
        return _seeded_choice(POKE_SUBJECTS, uid)
    # wesley -> sea animal, constrained to ones that can do the action
    pool = SEA_COHERENT.get(action, SEA_SUBJECTS)
    return _seeded_choice(pool, uid)


def build_combo(title: str, participant: str, uid: str) -> list[str]:
    """Return search queries (primary first) pairing subject + event action.

    A model-reasoning override (``LMAO_IMAGE_LLM_URL``) can supply richer
    phrase; otherwise the deterministic reasoned fallback is used.
    """
    action = extract_action(title)
    subject = pick_subject(participant, action, uid)
    primary = f"{subject} {action}".strip() if action else subject
    queries = [primary, subject]
    url = os.environ.get("LMAO_IMAGE_LLM_URL")
    if url:
        try:
            llm_q = _llm_keywords(url, title, participant, subject, action)
            if llm_q:
                return (llm_q if llm_q[0] else [primary]) + [subject]
        except Exception as exc:  # noqa: BLE001
            print(f"  (LLM keyword reasoning unavailable: {exc})")
    return queries


def _extract_json_array(text: str) -> list[str]:
    """Robustly pull a JSON array (possibly wrapped in prose/code fences)."""
    import json as _json

    text = text.strip()
    # strip markdown code fences
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        arr = _json.loads(text[start:end + 1])
    except _json.JSONDecodeError:
        return []
    if isinstance(arr, list):
        return [str(x).strip() for x in arr if str(x).strip()]
    return []


def _llm_keywords(url: str, title: str, participant: str, subject: str, action: str) -> list[str]:
    base = url.rstrip("/")
    endpoint = base + "/chat/completions" if base.endswith("/v1") else base + "/v1/chat/completions"
    theme = "pokemon" if participant == "juliet" else "sea animal"
    sys_msg = (
        "You choose image-search queries for a kid's family calendar tile. "
        "Given the theme character and the event, return 2-3 concise image-search "
        "queries pairing the character with the event action. Reject physically "
        "impossible pairings (a legless fish cannot ice-skate) and prefer a "
        "coherent character that can do the action. Reply with ONLY a JSON array "
        "of strings, e.g. [\"seal ice skating\", \"cartoon penguin on ice\"]. No "
        "prose, no code fences."
    )
    body = json.dumps({
        "model": os.environ.get("LMAO_IMAGE_LLM_MODEL", "deepseek-ai/DeepSeek-V4-Flash-0731"),
        "messages": [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": f"Theme: {subject} ({theme}). Event: {title}."},
        ],
        "temperature": 0.7,
        "max_tokens": 768,  # reasoning models spend tokens on CoT before content
    }).encode()
    headers = {"Content-Type": "application/json", "User-Agent": _UA}
    if os.environ.get("LMAO_IMAGE_LLM_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['LMAO_IMAGE_LLM_KEY']}"
    req = urllib.request.Request(endpoint, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=180) as resp:
        payload = json.load(resp)
    message = payload["choices"][0]["message"]
    text = (message.get("content") or "").strip()
    if not text:
        # reasoning model that stopped before emitting content — no usable output
        return []
    return _extract_json_array(text)


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


def search_google(query: str, limit: int = 5):
    """Google Custom Search Image API; needs GOOGLE_CSE_KEY + GOOGLE_CSE_CX."""
    key = os.environ.get("GOOGLE_CSE_KEY")
    cx = os.environ.get("GOOGLE_CSE_CX")
    if not key or not cx:
        return []
    url = "https://www.googleapis.com/customsearch/v1?" + urllib.parse.urlencode({
        "key": key, "cx": cx, "q": query, "searchType": "image",
        "num": limit, "safe": "active",
    })
    try:
        data = _get_json(url)
    except urllib.error.HTTPError as e:
        if e.code == 403:  # bad key/quota
            print("  (Google Images: quota/key 403 — using Commons only)")
            return []
        raise
    out = []
    for it in data.get("items", []) or []:
        mime = (it.get("mimeType") or "image/jpeg")
        if mime.startswith("image/"):
            img = it.get("image", {}) or {}
            out.append((it.get("title", ""), it.get("link", ""),
                        int(img.get("width", 0) or 0), int(img.get("height", 0) or 0)))
    return out


def search_sources(query: str):
    results = list(search_commons(query))
    results += search_google(query)
    return results


def pick_source(results, query: str = ""):
    """Pick the most usable candidate anchored to *query*, or None if none fit.

    A candidate is REJECTED unless its title contains at least one token of the
    primary query — so we never store a big random photo ("pokemon-livery
    airplane" for "goldeen tennis").  Among fits, the SUBJECT (first query word)
    match scores highest (a seal beats a human-skater painting for "seal ice
    skating"), and matching the action/other tokens adds more.
    """
    def norm(t: str) -> str:
        import unicodedata
        t = unicodedata.normalize("NFKD", t.lower())
        t = "".join(c for c in t if not unicodedata.combining(c))
        return "".join(c for c in t if c.isalnum())

    words = (query or "").split()
    subject = norm(words[0]) if words else ""
    tokens = [norm(t) for t in words if len(t) >= 4]

    def score(r):
        title, url, w, h = r
        if not url or not w or not h:
            return None
        if w > 6000 or h > 6000 or max(w, h) / max(1, min(w, h)) > 3.0:
            return None
        tn = norm(title)
        if tokens and not any(t in tn for t in tokens):
            return None  # no query token in the title -> unrelated, reject
        s = 0
        if subject and subject in tn:
            s += 60  # the character/subject is present
        for t in tokens:
            if t in tn and t != subject:
                s += 25  # matches the action / other query words too
        if "map" in tn or "diagram" in tn or "seating" in tn:
            s -= 20
        if 300 <= w <= 4000:
            s += 2
        if 0.5 <= min(w, h) / max(1, max(w, h)):
            s += 1
        return s

    scored = [(score(r), r) for r in results]
    scored = [(s, r) for s, r in scored if s is not None]
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


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


def kid_events_needing_pics(cal, pic_store, force: bool = False) -> list[tuple]:
    have = set(pic_store.keys()) if not force else set()
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
    parser.add_argument("--force", action="store_true",
                        help="regenerate pics even if the pic key is already stored")
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

    needed = kid_events_needing_pics(cal, pic_store, force=args.force)
    if args.dry_run:
        print(f"=== {len(needed)} kid event(s) need a pic ===")
        for uid, title, pic, participants in needed:
            participant = participants[0] if participants else "kid"
            print(f"  {pic}  {','.join(participants):<14} {title} -> "
                  f"queries={build_combo(title, participant, uid)}")
        pic_store.close()
        cal.close()
        return 0

    google_on = bool(os.environ.get("GOOGLE_CSE_KEY") and os.environ.get("GOOGLE_CSE_CX"))
    done, failed = 0, 0
    for uid, title, pic, participants in needed:
        participant = participants[0] if participants else "kid"
        queries = build_combo(title, participant, uid)
        primary = queries[0]
        kw = "LLM" if os.environ.get("LMAO_IMAGE_LLM_URL") else "reasoned"
        print(f"== {pic} ({','.join(participants)}): '{title}' "
              f"-> [{kw}] {queries} [commons{' + google' if google_on else ''}]")
        try:
            allres = []
            for q in queries:
                allres += search_sources(q)
            # dedupe by url, keep order
            seen, uniq = set(), []
            for r in allres:
                if r[1] and r[1] not in seen:
                    seen.add(r[1])
                    uniq.append(r)
            src = pick_source(uniq, primary)
            if src is None:
                print(f"   no usable image for {primary!r}")
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
            print(f"   FAILED for {primary!r}: {exc}")
            failed += 1
        time.sleep(1.0)
    print(f"=== done: {done} stored, {failed} failed ===")
    pic_store.close()
    cal.close()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
