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
endpoint, a small prompt asks it to turn the seeded theme subject + event action
into 2-3 clean line-drawing search queries (the subject is fixed per event so
recurring events vary); otherwise a deterministic reasoned fallback builds them.

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


def pick_subject(participant: str, action: str, uid: str) -> str:
    """A coherent, seeded per-event subject for the participant's theme."""
    if participant == "juliet":
        return _seeded_choice(POKE_SUBJECTS, uid)
    # wesley -> sea animal, constrained to ones that can do the action
    pool = SEA_COHERENT.get(action, SEA_SUBJECTS)
    return _seeded_choice(pool, uid)


def build_combo(title: str, participant: str, uid: str) -> list[str]:
    """Return search queries (primary first) pairing subject + event action.

    When ``LMAO_IMAGE_LLM_URL`` is set, the local model picks the best THEME
    subject + action combo for the event and returns the search queries it
    thinks will produce clean line drawings (always called — tokens are free on
    the local box; if the reasoning model spins out and returns nothing, we
    gracefully fall back to the deterministic combo).  Otherwise a deterministic
    reasoned fallback is used.
    """
    action = extract_action(title)
    subject = pick_subject(participant, action, uid)
    primary = f"{subject} {action}".strip() if action else subject
    queries = [primary, subject]
    url = os.environ.get("LMAO_IMAGE_LLM_URL")
    if url:
        try:
            combo = _llm_combo(url, title, participant, subject, action)
            if combo:
                act = combo.get("action") or action
                base = f"{subject} {act}".strip() if act else subject
                out = [base, subject]
                for q in (combo.get("queries") or []):
                    if q not in out:
                        out.append(q)
                return out
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


def _llm_combo(url: str, title: str, participant: str, subject: str, action: str) -> dict:
    """Ask the local model for clean line-drawing queries for the chosen subject.

    ``subject`` is the uid-seeded per-event pick (guarantees variety across
    events); the model must keep it and may only refine the action / phrasing,
    so a recurring event does not collapse to the same character every time.
    """
    base = url.rstrip("/")
    endpoint = base + "/chat/completions" if base.endswith("/v1") else base + "/v1/chat/completions"
    theme = "pokemon" if participant == "juliet" else "sea animal"
    action_hint = action or "none — invent a simple visual one the character can do that reads well as a line drawing"
    sys_msg = (
        "You write image-search queries for a kid's family-calendar picture tile. "
        f"The kid's theme is {theme}. A SUBJECT has already been chosen "
        "(varied per event) and you MUST keep it EXACTLY: {subject!r}. The "
        f"event action is {action_hint}. Return 2-3 queries for clean, simple "
        "LINE DRAWINGS of that subject doing the action, varying your wording "
        f"(e.g. \"{subject} {action or 'line drawing'}\"). Keep the subject "
        "exactly as given in every query; never substitute another character. "
        "Avoid physically impossible pairings. Reply with ONLY a JSON object: "
        '{"subject":"...","action":"...","queries":["a","b","c"]}. No prose, no '
        "code fences."
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


_GEN_URL_DEFAULT = "http://192.168.10.37:8188"


def search_sources(query: str):
    """Wikimedia Commons only — Google CSE is discontinued for new signups;
    on-demand themed art comes from local generation instead."""
    return list(search_commons(query))


def build_gen_prompt(subject_word: str, theme_label: str, action: str) -> str:
    theme = ("a cute sea animal" if theme_label == "sea animal"
             else "a cute pokemon-like creature")
    activity = f" doing {action}" if action else " doing a fun activity"
    return (
        f"{subject_word}, {theme}{activity}, flat BLACK line art on a WHITE "
        "background, clean bold black outline, coloring book style, no shading, "
        "no text, simple, kid-friendly"
    )


def generate_local(prompt: str, width: int, height: int) -> bytes | None:
    """Generate an image on the local ComfyUI node; return PNG bytes or None.

    Uses the FLUX.1-schnell checkpoint the node is set up with (falls back to
    the caller's search path if ComfyUI is down, the checkpoint isn't ready, or
    generation fails).
    """
    base = os.environ.get("LMAO_IMAGE_GEN_URL", _GEN_URL_DEFAULT).rstrip("/")
    # FLUX.1-schnell: transformer-only checkpoint + separate clip/t5/vae models.
    # This exact graph is verified on the node (ComfyUI docs FLUX template).
    workflow = {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": "flux1-schnell.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "DualCLIPLoader",
              "inputs": {"clip_name1": "clip_l.safetensors",
                         "clip_name2": "t5xxl_fp8_e4m3fn.safetensors",
                         "type": "flux"}},
        "3": {"class_type": "VAELoader",
              "inputs": {"vae_name": "ae.safetensors"}},
        "4": {"class_type": "CLIPTextEncode",
              "inputs": {"text": prompt, "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode",
              "inputs": {"text": "text, watermark, logo, lowres, blurry, photograph, shading, gray, realistic", "clip": ["2", 0]}},
        "6": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "7": {"class_type": "KSampler",
              "inputs": {
                  "model": ["1", 0], "positive": ["4", 0], "negative": ["5", 0],
                  "latent_image": ["6", 0], "seed": 0, "steps": 4, "cfg": 1.0,
                  "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"images": ["8", 0], "filename_prefix": "kids"}},
    }
    try:
        req = urllib.request.Request(
            f"{base}/prompt", data=json.dumps({"prompt": workflow}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": _UA},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            pid = json.load(resp)["prompt_id"]
        # poll /history until the job completes and an image is written
        out_image = None
        for _ in range(240):  # up to ~4 min
            time.sleep(1)
            try:
                with urllib.request.urlopen(f"{base}/history/{pid}", timeout=20) as r:
                    hist = json.load(r)
            except Exception:
                continue
            entry = (hist.get(pid) or {})
            outputs = entry.get("outputs", {})
            for node in outputs.values():
                for im in node.get("images", []) or []:
                    out_image = im
            if entry.get("status", {}).get("completed"):
                break
        if not out_image:
            return None
        view = (f"{base}/view?filename={urllib.parse.quote(out_image['filename'])}"
                f"&subfolder={urllib.parse.quote(out_image.get('subfolder', ''))}"
                f"&type={out_image.get('type', 'output')}")
        with urllib.request.urlopen(view, timeout=30) as r:
            return r.read()
    except Exception as exc:  # noqa: BLE001
        print(f"  (local generation unavailable: {exc})")
        return None


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


def to_1bit_png(raw: bytes, width: int, height: int, threshold: int) -> bytes:
    img = Image.open(io.BytesIO(raw)).convert("L")
    img = img.resize((width, height), Image.Resampling.LANCZOS)
    img = img.point(lambda v: 255 if v >= threshold else 0, mode="1")
    buf = io.BytesIO()
    img.save(buf, format="PNG", bits=1, optimize=True)
    return buf.getvalue()


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


def _parse_match(text: str):
    """Extract a boolean verdict from a vision reply, or None if unparseable."""
    obj = _extract_json_object(text)
    if obj and "match" in obj:
        v = obj["match"]
        return v if isinstance(v, bool) else str(v).strip().lower() in ("true", "yes", "1")
    return None


def verify_subject(raw: bytes, subject: str, theme: str) -> bool:
    """Confirm the image's MAIN subject actually depicts the intended theme.

    Fail-open: if the vision box is unavailable or gives no clear verdict, allow
    the image (consistent with the crop step). If it clearly says the subject is
    unrelated (e.g. a boat named "Slowpoke" instead of a pokemon), reject.
    """
    if Image is None:
        return True
    base = os.environ.get("LMAO_IMAGE_VISION_URL", _VISION_BASE_DEFAULT).rstrip("/")
    model = os.environ.get("LMAO_IMAGE_VISION_MODEL", _VISION_MODEL_DEFAULT)
    import base64 as _b64

    b64 = _b64.b64encode(raw).decode()
    endpoint = base + "/chat/completions" if base.endswith("/v1") else base + "/v1/chat/completions"
    prompt = (
        f"Is the MAIN subject of this image a {theme} resembling '{subject}' — i.e. "
        "the intended character/topic for a kid's calendar tile? If it is an "
        "unrelated object that merely shares the name (a boat named 'Slowpoke', a "
        "random person, a map), answer false. Reply ONLY a JSON object: "
        '{"match": true or false, "reason": "one short clause"}.' 
    )
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
    verdict = _parse_match(text)
    if verdict is None:
        print("  (vision content gate: no clear verdict — allowing)")
        return True
    if not verdict:
        print(f"  (vision content gate REJECTED: not a {theme})")
    return verdict


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

    done, failed = 0, 0
    for uid, title, pic, participants in needed:
        participant = participants[0] if participants else "kid"
        queries = build_combo(title, participant, uid)
        primary = queries[0]
        kw = "LLM" if os.environ.get("LMAO_IMAGE_LLM_URL") else "reasoned"
        subject_word = primary.split()[0] if primary.split() else participant
        theme_label = "pokemon" if participant == "juliet" else "sea animal"
        action = extract_action(title)
        print(f"== {pic} ({','.join(participants)}): '{title}' "
              f"-> [{kw}] {queries} [local-gen + commons]")
        try:
            stored = False
            # 1) on-demand local generation first (bespoke themed art)
            gen = generate_local(build_gen_prompt(subject_word, theme_label, action),
                                 1024, 1024)
            if gen:
                gen = crop_to_subject(gen)  # tighten to the subject (vision)
                if verify_subject(gen, subject_word, theme_label):
                    png = to_1bit_png(gen, tw, th, args.threshold)
                    rec = pic_store.put(pic, png, tw, th)
                    print(f"   stored {pic} [generated]: {subject_word} {theme_label}"
                          f" ({action or 'general'}) -> "
                          f"{rec['width']}x{rec['height']} {len(rec['bytes'])}B")
                    done += 1
                    stored = True
                else:
                    print(f"   gate rejected generated art for {primary!r}")
            else:
                print("   (local generation unavailable — falling back to Commons)")
            # 2) fall back to Wikimedia search if generation didn't land
            if not stored:
                allres = []
                for q in queries:
                    allres += search_sources(q)
                seen, uniq = set(), []
                for r in allres:
                    if r[1] and r[1] not in seen:
                        seen.add(r[1])
                        uniq.append(r)
                candidates = rank_candidates(uniq, primary)
                for cand in candidates:
                    _, url, w, h = cand
                    try:
                        raw = download(url)
                        raw = crop_to_subject(raw)  # tighten to the subject (vision)
                        if not verify_subject(raw, subject_word, theme_label):
                            print(f"   gate rejected candidate ({w}x{h}): {cand[0]!r}")
                            continue
                        png = to_1bit_png(raw, tw, th, args.threshold)
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
