#!/usr/bin/env python3
"""Line-drawing picture generator + Postgres store for the family calendar.

The skill tooling a Hermes-class agent uses to produce a calendar picture:
takes an SVG or PNG source (an existing ``assets/calendar-icons/<key>.svg`` is
used automatically), renders it **1-bit black & white** at the RLCD tile size,
and upserts the resulting PNG bytes into the in-cluster Postgres ``cal_pics``
table keyed by ``CalEvent.pic``.  Rerunning with the same key overwrites
(idempotent) — regenerating artwork for kids' calendars is the primary use.

Usage (agent-facing; see .archon/skills/line-drawing/SKILL.md):
    PYTHONPATH=<repo-root> python3 tools/calendar_pics.py --key swim
    PYTHONPATH=<repo-root> python3 tools/calendar_pics.py --key shopping \\
        --source my-art.svg --size 144x144 --out check.png

Host prerequisites (pip, outside the pod — nothing runs in-cluster here):
    python3 -m pip install --break-system-packages Pillow cairosvg 'psycopg[binary]'
The DB DSN is read from the same LMAO_CONTACTS_URL / LMAO_CONTACTS_PG_* env
as the server.  Without Postgres configured (or psycopg missing), the tool
falls back to writing the PNG to --out and exits non-zero with a message.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from typing import Any

# Repo root on path so `from lma_core...` resolves when run as a plain script.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from lma_core.calendar_pics import (  # noqa: E402
    pick_tile_size,
)


def _load_png_bytes(
    source_path: str,
    width: int,
    height: int,
    threshold: int,
) -> bytes:
    """Render source (SVG or PNG) to a 1-bit B&W PNG of (width, height)."""
    lower = source_path.lower()
    if lower.endswith(".svg"):
        try:
            import cairosvg
        except ImportError as exc:  # pragma: no cover - host tool
            raise RuntimeError(
                "cairosvg is required for SVG sources — "
                "pip install cairosvg"
            ) from exc
        png = cairosvg.svg2png(
            url=source_path, output_width=width, output_height=height,
            background_color="white",
        )
        pil_source = png
    else:
        try:
            from PIL import Image  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - host tool
            raise RuntimeError(
                "Pillow is required for PNG sources — pip install Pillow"
            ) from exc
        with Image.open(source_path) as src:
            pil_source = src.convert("L").resize(
                (width, height), Image.Resampling.LANCZOS
            )

    return _pil_to_1bit_png(pil_source, width, height, threshold)


def _pil_to_1bit_png(
    rgb: Any, width: int, height: int, threshold: int,
) -> bytes:
    """Turn Pillow L/RGBA image (or raw cairosvg PNG bytes) into 1-bit PNG."""
    from PIL import Image  # noqa: PLC0415

    if isinstance(rgb, (bytes, bytearray)):
        img = Image.open(io.BytesIO(bytes(rgb))).convert("L")
    else:
        img = rgb.convert("L") if rgb.mode != "L" else rgb

    # Threshold to pure 1-bit B&W (no dithering) — crisper on a reflective LCD.
    img = img.point(lambda v: 255 if v >= threshold else 0, mode="1")
    img = img.resize((width, height), Image.Resampling.NEAREST)
    buf = io.BytesIO()
    img.save(buf, format="PNG", bits=1, optimize=True)
    return buf.getvalue()


def _default_source(pic_key: str) -> str | None:
    candidate = os.path.join(_REPO_ROOT, "assets", "calendar-icons", f"{pic_key}.svg")
    return candidate if os.path.isfile(candidate) else None


def _store(pic_key: str, png: bytes, width: int, height: int, out_path: str | None) -> int:
    if out_path:
        with open(out_path, "wb") as f:
            f.write(png)
        print(f"Wrote {len(png)} bytes to {out_path}")

    try:
        from lma_core.calendar_pics import open_pic_store  # noqa: PLC0415
    except ImportError:
        open_pic_store = None  # type: ignore[assignment]

    store = open_pic_store() if open_pic_store else None
    if store is None:
        print(
            "ERROR: Postgres not reachable (LMAO_CONTACTS_URL / LMAO_CONTACTS_PG_*"
            " unset or psycopg missing) — PNG written locally only.",
            file=sys.stderr,
        )
        return 1
    try:
        rec = store.put(pic_key, png, width, height)
        print(
            f"Stored {pic_key!r}: {rec['width']}x{rec['height']} "
            f"{rec['mime']} ({len(rec['bytes'])} bytes)"
        )
        return 0
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", help="CalEvent.pic key to store under (upsert)")
    parser.add_argument("--source", help="SVG or PNG source; defaults to assets/calendar-icons/<key>.svg")
    parser.add_argument("--size", default="144x144", help="tile WxH (RLCD 300x400 panel); default 144x144")
    parser.add_argument("--threshold", type=int, default=128, help="B&W threshold 0-255 (default 128)")
    parser.add_argument("--out", help="also write the PNG locally (always on missing-DB fallback)")
    parser.add_argument("--list", action="store_true", help="list stored picture keys and exit")
    args = parser.parse_args(argv)

    if args.list:
        try:
            from lma_core.calendar_pics import open_pic_store  # noqa: PLC0415
        except ImportError:
            open_pic_store = None  # type: ignore[assignment]
        store = open_pic_store() if open_pic_store else None
        if store is None:
            print("ERROR: no Postgres reachable.", file=sys.stderr)
            return 1
        try:
            store_keys = store.keys()
            for k in store_keys:
                print(k)
            return 0
        finally:
            store.close()
    if not args.key:
        parser.error("--key is required (or --list)")

    try:
        width_s, height_s = args.size.lower().split("x")
        width, height = int(width_s), int(height_s)
    except (ValueError, AttributeError):
        parser.error("--size must be WxH, e.g. 144x144")

    source = args.source or _default_source(args.key)
    if not source:
        parser.error(
            f"no source for --key {args.key!r}: pass --source or add "
            f"assets/calendar-icons/{args.key}.svg"
        )

    try:
        # Full-size sanity: never exceed the panel tile contract.
        tile = pick_tile_size()
        width, height = min(width, tile[0]), min(height, tile[1])
        png = _load_png_bytes(source, width, height, args.threshold)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return _store(args.key, png, width, height, args.out)


if __name__ == "__main__":
    sys.exit(main())
