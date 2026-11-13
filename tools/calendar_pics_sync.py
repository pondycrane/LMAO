#!/usr/bin/env python3
"""Picture stage for the LMAO kid calendar: themed art -> RLCD tile in cal_pics.

For each *upcoming* kid event (``participants`` non-empty) whose ``pic`` key is
not yet in ``cal_pics``, this stage:

1. reasons about the best SEARCH QUERIES for that event (seeded per event),
2. searches the internet for LINE DRAWINGS of that subject (Wikimedia
   Commons, plus DuckDuckGo image search biased to line art / coloring pages),
3. thresholds the best line drawing to a 1-bit black-and-white 176x176 tile,
4. stores it under the event's ``pic`` key.

There is no image GENERATION step: the tiles are fetched line drawings.  A
subject with no acceptable online line drawing stays artless and is retried on
the next hourly run (or stored by hand with
``bazel run //tools:calendar_pics -- --key <key> --source <url>``).

Subjects are paired with the event's ACTION and are kept physically coherent
(a legless dolphin is not asked to ice-skate; Wesley's sea-animal theme falls
back to a limbed sea animal like a seal/otter/penguin, Juliet's pokemon theme
can play any sport).  The subject is chosen pseudo-randomly per event
(deterministic per ``uid`` so re-runs are idempotent) and is authoritative:
the model only refines the action and the search queries for that subject, so
Juliet gets a varied pokemon (not always Pikachu) and Wesley alternates
PAW Patrol characters with sea animals (not always sea animals).

Model reasoning: if ``LMAO_IMAGE_LLM_URL`` points at an OpenAI-compatible chat
endpoint, a small prompt asks it for an action + 2-3 image-search queries that
name the already-chosen subject; otherwise a deterministic reasoned fallback
builds them.

Idempotent: a ``pic`` key already present in ``cal_pics`` is skipped, so re-runs
only do new events (``--force`` regenerates).

Usage::
    PYTHONPATH=repo python3 tools/calendar_pics_sync.py [--dry-run] [--force]
    # Backfill recent tiles (e.g. after changing subject selection):
    PYTHONPATH=repo python3 tools/calendar_pics_sync.py --force --since-days 14
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
    "snorlax", "poliwag", "goldeen", "slowpoke", "eevee", "meowth",
    "growlithe", "lapras", "magikarp", "gengar", "onix", "vulpix",
]
SEA_SUBJECTS = [
    "dolphin", "orca", "sea turtle", "octopus", "crab", "jellyfish",
    "whale", "shark", "lobster", "seal", "sea lion", "otter", "penguin",
]
# Wesley's alternate theme (PAW Patrol): proprietary IP, so Wikimedia Commons
# carries no licensed art — the DuckDuckGo line-art/coloring-page search is
# what reaches it. Alternates with sea animals, seed-chosen per event.
PAW_SUBJECTS = [
    "chase", "marshall", "skye", "rubble", "rocky", "zuma", "everest", "tracker",
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
# Kept in lockstep with lma_core.calendar_pics.TILE_W/H (the panel's headline
# slot); the module's pick_tile_size() is the authority at run time.
_TILE_W, _TILE_H = 176, 176
_THRESHOLD = 140  # generous white so anti-aliased line art stays clean (no gray ink)

# Local Qwen-VL vision box for subject cropping. Default is the ML endpoint IP
# (the `spark-eefb` hostname fails to resolve from this Pi even though it is in
# /etc/hosts — the IP works). Override with LMAO_IMAGE_VISION_URL / _MODEL.
_VISION_BASE_DEFAULT = "http://192.168.50.200:8000/v1"
_VISION_MODEL_DEFAULT = "Qwen/Qwen2.5-VL-72B-Instruct"


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


def pick_theme(participant: str, action: str, uid: str) -> tuple[str, list[str]]:
    """Return (theme_label, subject_pool) for this event.

    Juliet is always pokemon.  Wesley alternates sea animals and PAW Patrol,
    seeded per event so a recurring title varies across occurrences; sea
    subjects are restricted to a limb-coherent pool for the event's action.
    """
    if participant == "juliet":
        return "pokemon", POKE_SUBJECTS
    rng = random.Random(hashlib.sha1((uid + ":theme").encode()).hexdigest())
    if rng.random() < 0.5:
        return "sea animal", SEA_COHERENT.get(action, SEA_SUBJECTS)
    return "paw patrol", PAW_SUBJECTS


def pick_subject(participant: str, action: str, uid: str) -> str:
    """A coherent, seeded per-event subject for the participant's theme."""
    _, pool = pick_theme(participant, action, uid)
    return _seeded_choice(pool, uid)


def _pool_for(subject: str, action: str) -> tuple[list[str], str]:
    """Map a picked subject to its (pool, theme_label) for the regen retry.

    Lets the generator retry with an alternate subject from the SAME theme pool
    when the vision gate false-rejects an obscure character (e.g. goldeen or
    alakazam), instead of giving up. ``theme_label`` is the truthful label for
    the vision gate (pokemon / paw patrol / sea animal).
    """
    if subject in POKE_SUBJECTS:
        return POKE_SUBJECTS, "pokemon"
    if subject in PAW_SUBJECTS:
        return PAW_SUBJECTS, "paw patrol"
    return SEA_COHERENT.get(action, SEA_SUBJECTS), "sea animal"


def build_combo(title: str, participant: str, uid: str) -> dict:
    """Pick the event's subject + action + search queries.

    The SUBJECT is chosen by us — seeded uniformly from the participant's
    theme pool — and is authoritative: the model never gets to override it
    (left free, it answers "pikachu" for every pokemon event, and never leaves
    Wesley's sea-animal theme).  When ``LMAO_IMAGE_LLM_URL`` is set, the model
    only refines the ACTION and proposes extra queries for that same subject;
    otherwise a deterministic combo is used.

    Returns ``{subject, theme, pool, action, queries}``; ``queries[0]`` is the
    subject+action primary search query.
    """
    action = extract_action(title)
    subject = pick_subject(participant, action, uid)
    pool, theme = _pool_for(subject, action)
    url = os.environ.get("LMAO_IMAGE_LLM_URL")
    if url:
        try:
            combo = _llm_combo(url, title, subject, theme)
            if combo:
                action = str(combo.get("action") or action).strip()
                extra = [q for q in combo.get("queries") or [] if _mentions(q, subject)]
                queries = _dedupe([_primary_query(subject, action)] + extra + [subject])
                return {"subject": subject, "theme": theme, "pool": pool,
                        "action": action, "queries": queries}
        except Exception as exc:  # noqa: BLE001
            print(f"  (LLM keyword reasoning unavailable: {exc})")
    return {"subject": subject, "theme": theme, "pool": pool, "action": action,
            "queries": [_primary_query(subject, action), subject]}


def _primary_query(subject: str, action: str) -> str:
    return f"{subject} {action}".strip() if action else subject


def theme_query(subject: str, theme: str, action: str) -> str:
    """Disambiguate a character name from an everyday word for a web search.

    ``chase tennis`` finds a tennis player named Chase; ``chase paw patrol
    tennis`` finds the PAW Patrol dog's coloring page.  Character themes carry
    no licensed Commons art, so the web index needs the theme name to land on
    the character rather than on a namesake.
    """
    if theme in ("paw patrol", "pokemon"):
        return f"{subject} {theme} {action}".strip()
    return _primary_query(subject, action)


def _mentions(query: str, subject: str) -> bool:
    """True when *query* actually names *subject* (accent/space-insensitive)."""
    return _norm_title(subject) in _norm_title(query)


def _dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for item in items:
        key = _norm_title(item)
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


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


def _extract_json_object(text: str) -> dict:
    """Robustly pull the first JSON object from a model reply."""
    import json as _json

    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        obj = _json.loads(text[start:end + 1])
    except _json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def _llm_combo(url: str, title: str, subject: str, theme: str) -> dict:
    """Ask the local model for an action + extra queries for the FIXED subject.

    The subject is chosen by :func:`pick_subject` and is not up for debate —
    the model only refines the action and proposes queries that name it.
    """
    base = url.rstrip("/")
    endpoint = base + "/chat/completions" if base.endswith("/v1") else base + "/v1/chat/completions"
    sys_msg = (
        "You choose image-search queries for a kid's family-calendar picture tile. "
        f"The kid's theme is {theme} and the subject for this event is ALREADY "
        f"chosen: '{subject}'. You MUST keep that exact subject — never substitute "
        "another character. Given the event, choose a concrete action word or "
        "short phrase the subject can clearly do, and 2-3 image-search queries "
        "that contain the subject and would return clean, simple LINE DRAWINGS of "
        "it doing that action (e.g. \"seal ice skating\", \"cartoon penguin on "
        "ice\"). If the event has no obvious action (e.g. an \"open house\"), "
        "invent a simple visual one that reads well as a line drawing. Avoid "
        "physically impossible pairings (a legless fish cannot ice-skate). Reply "
        "with ONLY a JSON object: {\"subject\":\"...\", \"action\":\"...\", "
        "\"queries\":[\"a\",\"b\",\"c\"]}. No prose, no code fences."
    )
    body = json.dumps({
        "model": os.environ.get("LMAO_IMAGE_LLM_MODEL", "deepseek-ai/DeepSeek-V4-Flash-0731"),
        "messages": [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": f"Event: {title}."},
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
        # reasoning model that spun on CoT and never emitted content
        return {}
    return _extract_json_object(text)


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


# ComfyUI / any local image generator is NOT used: tiles are fetched line
# drawings from the internet.  DuckDuckGo's image JSON API is keyless and is
# queried with a line-art bias so character tiles come back as coloring-page /
# line-drawing art rather than photographs.
_DDG_IMAGES = "https://duckduckgo.com/i.js"
_DDG_HOME = "https://duckduckgo.com/"
# Web-search line-art hints: "coloring page" for characters, "line drawing" for
# everything else (see search_sources).
_VQD_CACHE: dict[str, str] = {}
# Title/URL signals that a candidate is line art (coloring book, clipart,
# sketch) vs. a photograph.  Used by the ranker and the ink gate.
_LINE_ART_TITLE_HINTS = (
    "coloring", "lineart", "line art", "line-art", "line drawing",
    "line-drawing", "sketch", "clipart", "clip art", "outline", "vector",
)
_PHOTO_TITLE_HINTS = ("photo", "photograph", "real life", "stock photo")


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


def search_sources(query: str, theme: str = ""):
    """Internet line-drawing sources for one query.

    Wikimedia Commons carries public-domain line art (animals, objects); the
    DuckDuckGo image index reaches character line drawings (coloring pages)
    that Commons has no licensed copy of.  The web query is biased with ONE
    line-art hint — "coloring page" for characters, "line drawing" otherwise —
    to keep the hourly run inside DuckDuckGo's rate limit.  Ranking + the
    vision gate + the ink gate then pick the usable tile.
    """
    hint = "coloring page" if theme in ("paw patrol", "pokemon") else "line drawing"
    out = list(search_commons(query))
    try:
        out += search_web_images(f"{query} {hint}")
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
    # Line drawings are the ONLY acceptable tile source now (no image
    # generator), so a line-art signal must outrank a photo of the same
    # subject: "Chase Paw Patrol Coloring Page" beats a photo of a person
    # named Chase.  Photos survive the title filter but fail the ink gate.
    line_art = any(h in tn or h in url.lower() for h in _LINE_ART_TITLE_HINTS)
    if line_art:
        s += 80
    if any(h in tn for h in _PHOTO_TITLE_HINTS):
        s -= 40
    if 300 <= w <= 4000:
        s += 2
    if 0.5 <= min(w, h) / max(1, max(w, h)):
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
                coverage: float = 0.09) -> bytes:
    """Rasterize a line drawing to a 1-bit tile.

    Thresholding AFTER a LANCZOS downscale turns a detailed line drawing into
    gray mush (anti-aliased lines average above the threshold and vanish, while
    textured areas speckle).  Instead: threshold the source at full resolution
    to an ink mask, area-average that mask down to the tile (BOX = mean ink
    coverage per cell), then mark a cell as ink when it holds a real amount of
    ink (``coverage``).  Thin 1-px lines survive; gray noise does not.

    ``coverage`` is 0.09 so a single-pixel line in a 10x-downscaled source
    (BOX averages it to 25/255 ≈ 0.098 of the cell) still registers; photo /
    dither noise cells average far below that and drop out.
    """
    img = Image.open(io.BytesIO(raw)).convert("L")
    mask = img.point(lambda v: 255 if v < threshold else 0)
    small = mask.resize((width, height), Image.Resampling.BOX)
    tile = small.point(lambda v: 0 if v >= coverage * 255 else 255, mode="1")
    buf = io.BytesIO()
    tile.save(buf, format="PNG", bits=1, optimize=True)
    return buf.getvalue()


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


def _parse_bbox(text: str, width: int, height: int):
    """Parse a vision reply to a valid pixel bbox or None.

    Accepts a ``[x1,y1,x2,y2]`` array (or bare numbers). Clamps to the image,
    rejects degenerate/at-least-near-full-frame boxes (no point cropping) and
    overly tiny boxes.
    """
    import re

    m = re.search(r"\[\s*([0-9.,\s\-]+)\]", text)
    if m:
        import json as _json

        try:
            arr = _json.loads("[" + m.group(1) + "]")
        except _json.JSONDecodeError:
            arr = []
    else:
        arr = [int(t) for t in re.findall(r"-?\d+", text)][:4]
    if len(arr) < 4:
        return None
    try:
        x1, y1, x2, y2 = (int(v) for v in arr[:4])
    except (TypeError, ValueError):
        return None
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(width, x2), min(height, y2)
    if x2 <= x1 or y2 <= y1:
        return None
    if (x2 - x1) >= 0.98 * width and (y2 - y1) >= 0.98 * height:
        return None  # basically the full frame — nothing to crop
    if (x2 - x1) < 32 or (y2 - y1) < 32:
        return None  # degenerately small box
    return (x1, y1, x2, y2)


def crop_to_subject(raw: bytes) -> bytes:
    """Crop the downloaded image to its main subject via the local vision box.

    Returns the cropped re-encoded PNG, or ``raw`` unchanged if the vision box
    is unavailable or returns no usable box (never fatal).
    """
    if Image is None:
        return raw
    base = os.environ.get("LMAO_IMAGE_VISION_URL", _VISION_BASE_DEFAULT).rstrip("/")
    model = os.environ.get("LMAO_IMAGE_VISION_MODEL", _VISION_MODEL_DEFAULT)
    try:
        with Image.open(io.BytesIO(raw)) as _im0:
            W, H = _im0.size
    except Exception:
        return raw

    import base64 as _b64

    b64 = _b64.b64encode(raw).decode()
    endpoint = base + "/chat/completions" if base.endswith("/v1") else base + "/v1/chat/completions"
    prompt = (
        "Return ONLY a JSON array [x1,y1,x2,y2] with the PIXEL bounding box of "
        "the MAIN foreground subject in this image, so it can be cropped to "
        "fill the frame. If the subject already fills most of the frame, return "
        f"the full frame [0,0,{W},{H}]. No prose."
    )
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
        ]}],
        "max_tokens": 160,
    }).encode()
    req = urllib.request.Request(
        endpoint, data=body,
        headers={"Content-Type": "application/json", "User-Agent": _UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.load(resp)
        text = (payload["choices"][0]["message"].get("content") or "").strip()
    except Exception as exc:  # noqa: BLE001
        print(f"  (vision crop unavailable: {exc})")
        return raw

    bbox = _parse_bbox(text, W, H)
    if bbox is None:
        print("  (vision crop: no usable box — using full frame)")
        return raw
    x1, y1, x2, y2 = bbox
    with Image.open(io.BytesIO(raw)) as im:
        im = im.crop((x1, y1, x2, y2))
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        cropped = buf.getvalue()
    print(f"  (vision cropped to {x2 - x1}x{y2 - y1})")
    return cropped


def _parse_verdict(text: str) -> tuple[bool | None, str]:
    """Parse a gate reply into (verdict, character).  None = unparseable."""
    obj = _extract_json_object(text)
    if not obj or "match" not in obj:
        return None, ""
    v = obj["match"]
    verdict = v if isinstance(v, bool) else str(v).strip().lower() in ("true", "yes", "1")
    character = str(obj.get("character") or "").strip()
    return verdict, character


def _parse_match(text: str):
    """Extract a boolean verdict from a vision reply, or None if unparseable."""
    return _parse_verdict(text)[0]


def _accept_names(theme: str, accept: list[str] | None) -> list[str]:
    if accept:
        return list(accept)
    return PAW_SUBJECTS if theme == "paw patrol" else SEA_SUBJECTS


def build_verify_prompt(subject: str, theme: str,
                        accept: list[str] | None = None) -> str:
    """Gate prompt for *theme* — theme-level, not name-exact, for pokemon.

    The old prompt demanded the image resemble the EXACT chosen character
    ("a pokemon resembling 'charmander'").  At tile size the vision model cannot
    reliably name a specific Pokémon, so valid renders (charmander, poliwag,
    goldeen, magikarp, slowpoke) were rejected and the retry loop walked the
    pool until something passed — which landed on Gengar over and over.
    For pokemon, ANY Pokémon character is acceptable; the reply names which
    one, so the tile stays traceable.  PAW Patrol keeps name-exactness (the
    kid knows the dogs) and sea animals keep the action-coherent pool.
    """
    reply = ('Reply ONLY a JSON object: {"match": true or false, '
             '"character": "its name if recognizable, else unknown"}.')
    if theme == "pokemon":
        return (
            "Is the MAIN subject of this image a Pokémon character — a drawn/cartoon "
            "creature from the Pokémon franchise? A photograph, a person in a costume, "
            "an unrelated object that merely shares the name (a boat named 'Slowpoke'), "
            "or a map does NOT count. It does NOT need to be exactly "
            f"'{subject}' — any Pokémon character is acceptable. " + reply
        )
    if theme == "paw patrol":
        names = ", ".join(_accept_names(theme, accept))
        return (
            "Is the MAIN subject of this image a PAW Patrol dog character — one of "
            f"{names}? A real dog photo, a random person, or an unrelated object that "
            "merely shares the name does NOT count. " + reply
        )
    names = ", ".join(_accept_names(theme, accept))
    return (
        "Is the MAIN subject of this image one of these sea animals: "
        f"{names}? A random person, a map, or an unrelated object that merely shares "
        "the name does NOT count. " + reply
    )


def verify_subject(raw: bytes, subject: str, theme: str,
                   accept: list[str] | None = None) -> bool:
    """Confirm the image's MAIN subject fits the theme.

    Theme-level for pokemon (any Pokémon counts, and the identified character is
    logged), name-exact for PAW Patrol, and restricted to the action-coherent
    pool for sea animals.  Fail-open: an unavailable vision box or an unclear
    verdict allows the image (consistent with the crop step).  A clear negative
    (a cosplay photo, a boat named "Slowpoke", a map) rejects it.
    """
    if Image is None:
        return True
    base = os.environ.get("LMAO_IMAGE_VISION_URL", _VISION_BASE_DEFAULT).rstrip("/")
    model = os.environ.get("LMAO_IMAGE_VISION_MODEL", _VISION_MODEL_DEFAULT)
    import base64 as _b64

    b64 = _b64.b64encode(raw).decode()
    endpoint = base + "/chat/completions" if base.endswith("/v1") else base + "/v1/chat/completions"
    prompt = build_verify_prompt(subject, theme, accept)
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
        ]}],
        "max_tokens": 200,
    }).encode()
    req = urllib.request.Request(
        endpoint, data=body,
        headers={"Content-Type": "application/json", "User-Agent": _UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.load(resp)
        text = (payload["choices"][0]["message"].get("content") or "").strip()
    except Exception as exc:  # noqa: BLE001
        print(f"  (vision content gate unavailable: {exc})")
        return True
    verdict, character = _parse_verdict(text)
    if verdict is None:
        print("  (vision content gate: no clear verdict — allowing)")
        return True
    if not verdict:
        print(f"  (vision content gate REJECTED: not a {theme})")
    elif character and character.lower() != "unknown":
        print(f"  (gate accepted: {theme} -> {character})")
    return verdict


def kid_events_needing_pics(cal, pic_store, force: bool = False,
                            since_days: int = 0, limit: int = 0) -> list[tuple]:
    """Events missing a pic; ``since_days`` also includes recent (past) events.

    Default window is future events only.  ``--force --since-days N`` is the
    backfill path: every kid event that started within the last N days (or is
    still upcoming) is regenerated, so a tile picked under the old
    always-Pikachu rule gets replaced.  ``limit`` caps the batch, nearest
    events first.
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
        for uid, title, pic, participants in needed:
            participant = participants[0] if participants else "kid"
            combo = build_combo(title, participant, uid)
            print(f"  {pic}  {','.join(participants):<14} {title} -> "
                  f"{combo['subject']} ({combo['theme']}, "
                  f"{combo['action'] or 'general'}) queries={combo['queries']}")
        pic_store.close()
        cal.close()
        return 0

    done, failed = 0, 0
    for uid, title, pic, participants in needed:
        participant = participants[0] if participants else "kid"
        combo = build_combo(title, participant, uid)
        queries = combo["queries"]
        primary = queries[0]
        kw = "LLM" if os.environ.get("LMAO_IMAGE_LLM_URL") else "reasoned"
        action = combo["action"]
        subject_word = combo["subject"]
        pool, theme_label = combo["pool"], combo["theme"]
        print(f"== {pic} ({','.join(participants)}): '{title}' "
              f"-> [{kw}] {subject_word} ({theme_label}) {queries} "
              f"[commons + web line-art]")
        try:
            stored = False
            # Search the internet for a LINE DRAWING of the chosen subject.
            # On a vision-gate rejection, retry one alternate subject from the
            # SAME theme pool: an obscure subject (an unrecognized pokemon) may
            # have no clean line drawing online while its pool-mates do.
            base_seed = int(hashlib.sha1(uid.encode()).hexdigest(), 16)
            order = [subject_word] + [pool[(base_seed + i) % len(pool)]
                                      for i in range(1, len(pool))]
            uniq_order, seen_s = [], set()
            for s_ in order:
                if s_ not in seen_s:
                    seen_s.add(s_)
                    uniq_order.append(s_)
            for subj in uniq_order[:2]:
                subj_queries = [theme_query(subj, theme_label, action)]
                if subj == subject_word:
                    subj_queries += [q for q in queries if q != primary]
                subj_queries.append(subj)
                subj_queries = _dedupe(subj_queries)
                allres = []
                for q in subj_queries:
                    allres += search_sources(q, theme_label)
                seen, uniq = set(), []
                for r in allres:
                    if r[1] and r[1] not in seen:
                        seen.add(r[1])
                        uniq.append(r)
                candidates = rank_candidates(uniq, subj_queries[0])
                for cand in candidates[:8]:
                    _, url, w, h = cand
                    try:
                        raw = download(url)
                        raw = crop_to_subject(raw)  # tighten to the subject (vision)
                        if not verify_subject(raw, subj, theme_label, accept=pool):
                            print(f"   gate rejected candidate ({w}x{h}): {cand[0]!r}")
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
                if stored:
                    break
                if subj == subject_word:
                    print(f"   no acceptable {subject_word!r} line drawing —"
                          " trying another subject from the same theme...")
            if not stored:
                print(f"   no acceptable image for {primary!r}")
                failed += 1
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
