"""Postgres-backed store for family-calendar line-drawing pictures.

Event pictures (``CalEvent.pic`` keys) are small **1-bit black-and-white**
PNGs, generated out-of-band by the ``tools/calendar_pics.py`` skill tooling
(an agent rasterizes an SVG/PNG source — a local file or a small line drawing
found online — at the RLCD tile size, converts it to black/white, and upserts
the bytes here).  This module only *stores and serves* those bytes — no image
processing runs in the server pod.

Storage contract (RLCD display constraints)
-------------------------------------------
- Target display: 4.2" reflective LCD, 300x400 portrait.
- A picture is a content tile, deliberately smaller than the full panel so
  the screen still has room for the title/time/date rows:
  :func:`pick_tile_size` defaults to 176x176 (the headline drawing, drawn at
  native size in the panel's 400x300 landscape layout).
- Format: 1-bit PNG (``mode '1'``).  At 176x176 a compact line drawing is a
  few hundred bytes; 1-bit renders crisply on a reflective panel.

Schema: ``cal_pics`` (keyed by the same ``pic`` string ``cal_events`` refers
to, so one picture can back many events).  Mirrors ``calendar_pg`` /
``contact_book_pg`` conventions: lazily imported psycopg, one shared
connection + lock, ensure-schema on init, DSN from
``contact_book_pg.compose_postgres_dsn``.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from lma_core.contact_book_pg import compose_postgres_dsn

_logger = logging.getLogger(__name__)

try:  # psycopg 3
    import psycopg
except ImportError:  # pragma: no cover - dev/test envs without Postgres
    psycopg = None

# Contract values for the RLCD tile size (see module docstring).
SCREEN_W = 300
SCREEN_H = 400
DEFAULT_TILE_W = 176
DEFAULT_TILE_H = 176


def pick_tile_size(screen_w: int = SCREEN_W, screen_h: int = SCREEN_H) -> tuple[int, int]:
    """Return (width, height) for a calendar picture on the 4.2" RLCD panel.

    The picture is a tile inside the event row: it must leave room for the
    title/time/date text.  Defaults to a 176x176 square (the headline slot in
    the 400x300 landscape layout, RD_PIC_TILE in rd_layout.h); the smaller of
    the two dimensions governs so the tile never exceeds the panel.
    """
    return (min(DEFAULT_TILE_W, screen_w - 20), min(DEFAULT_TILE_H, screen_h - 20))


# ---------------------------------------------------------------------------
# Rasterizing a line drawing into the tile
# ---------------------------------------------------------------------------
# The tile is square, the sources are not: a coloring page is typically
# portrait (700x900, 864x1184) and a naive square resize squashes the figure
# into an unrecognizable blob.  These constants drive the shared rasterizer
# used by BOTH the hourly sync stage and the by-hand tool:
#   - INK_THRESHOLD: generous white so anti-aliased line art keeps its ink;
#   - INK_COVERAGE: a downscaled cell holding at least this much ink stays
#     black, which is what lets a 1-px line survive a 10x downscale;
#   - TILE_MARGIN: white breathing room inside the tile, tile pixels;
#   - MAX_CONTENT_ASPECT: a drawing whose ink bbox is more than this wide or
#     tall (a banner, a row of panels) is a sliver at tile size;
#   - SUBJECT_MIN_BBOX_FRAC: components smaller than this share of the page
#     are furniture, not the drawing (see subject_bbox).
INK_THRESHOLD = 140
INK_COVERAGE = 0.09
TILE_MARGIN = 6
MAX_CONTENT_ASPECT = 2.6
# A connected ink component smaller than this fraction of the page area is
# page furniture (a footer logo, a page number, a speck), not the drawing.
SUBJECT_MIN_BBOX_FRAC = 0.04


def ink_mask(img: Any, threshold: int = INK_THRESHOLD) -> Any:
    """Threshold a Pillow image to an ink mask (255 = ink, 0 = white)."""
    return img.convert("L").point(lambda v: 255 if v < threshold else 0)


def _require_pillow():
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - host tooling has Pillow
        raise RuntimeError(
            "Pillow is required to rasterize a calendar picture — "
            "pip install Pillow"
        ) from exc
    return Image


def subject_mask(raw: bytes, threshold: int = INK_THRESHOLD,
                 min_bbox_frac: float = SUBJECT_MIN_BBOX_FRAC):
    """Ink mask of the DRAWING only: (mask, bbox), page furniture removed.

    A plain ink bbox keeps whatever else the page carries — a site footer
    logo, a page number, stray specks — and those land in the tile as black
    blobs beside the figure.  So the ink is labelled into connected components
    on a 2x-downscaled mask (BOX keeps a 1-px line above the cut), components
    whose bbox is a real part of the page (>= ``min_bbox_frac`` of the image
    area) are kept, and the kept mask is scaled back and intersected with the
    full-resolution ink so line weight is unchanged.

    Returns ``(mask_image, (x1, y1, x2, y2))`` in source pixels; ``(None,
    None)`` when the source has no ink.
    """
    import io  # noqa: PLC0415
    from collections import deque  # noqa: PLC0415

    from PIL import ImageChops  # noqa: PLC0415

    pil = _require_pillow()
    with pil.open(io.BytesIO(raw)) as im:
        full = ink_mask(im, threshold)
    w, h = full.size
    down = 2
    sm = full.resize((max(1, w // down), max(1, h // down)),
                     pil.Resampling.BOX).point(lambda v: 255 if v >= 64 else 0)
    sw, sh = sm.size
    px = sm.load()
    seen = bytearray(sw * sh)
    min_area = min_bbox_frac * sw * sh
    keep = bytearray(sw * sh)
    found = False
    for i in range(sw * sh):
        if seen[i] or px[i % sw, i // sw] == 0:
            continue
        queue = deque([i])
        seen[i] = 1
        cells = []
        x1 = y1 = sw + sh
        x2 = y2 = -1
        while queue:
            j = queue.popleft()
            cells.append(j)
            x, y = j % sw, j // sw
            x1, y1, x2, y2 = min(x1, x), min(y1, y), max(x2, x), max(y2, y)
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1),
                           (x - 1, y - 1), (x + 1, y + 1), (x - 1, y + 1), (x + 1, y - 1)):
                if 0 <= nx < sw and 0 <= ny < sh:
                    k = ny * sw + nx
                    if not seen[k] and px[nx, ny]:
                        seen[k] = 1
                        queue.append(k)
        if (x2 - x1 + 1) * (y2 - y1 + 1) >= min_area:
            found = True
            for j in cells:
                keep[j] = 1
    if not found:
        return None, None
    kept = pil.frombytes("L", (sw, sh), bytes(255 if k else 0 for k in keep))
    up = kept.resize((w, h), pil.Resampling.NEAREST)
    mask = ImageChops.logical_and(up.convert("1"), full.convert("1"))
    mask = mask.convert("L").point(lambda v: 255 if v else 0)
    bbox = mask.getbbox()
    if bbox is None:
        return None, None
    return mask, bbox


def content_aspect(raw: bytes, threshold: int = INK_THRESHOLD) -> float | None:
    """Width/height of the source's subject bbox, or None when it has none.

    Used to reject strip-shaped hits (a banner or a multi-panel print sheet)
    before they are shrunk to a speck.
    """
    _, bbox = subject_mask(raw, threshold)
    if bbox is None:
        return None
    return (bbox[2] - bbox[0]) / max(1, bbox[3] - bbox[1])


def rasterize_line_art(raw: bytes, width: int = DEFAULT_TILE_W,
                       height: int = DEFAULT_TILE_H,
                       threshold: int = INK_THRESHOLD,
                       margin: int = TILE_MARGIN) -> bytes:
    """Render a line drawing to a 1-bit PNG tile, aspect-preserving.

    1. trim the source to its SUBJECT bbox (:func:`subject_bbox`): the page's
       white border, a footer bar or a page number would otherwise ride along
       and land in the tile as a black blob beside the drawing;
    2. threshold at full resolution and area-average the ink mask down (BOX =
       mean ink coverage per cell), marking a cell black only when it holds a
       real amount of ink — thin 1-px lines survive, photo noise does not;
    3. scale by the smaller of the two fits and CENTER the result on a white
       tile, so the drawing keeps its own proportions.

    Returns the 1-bit PNG bytes (a blank tile when the source has no ink).
    """
    import io  # noqa: PLC0415

    pil = _require_pillow()
    mask, bbox = subject_mask(raw, threshold)
    buf = io.BytesIO()
    if mask is None or bbox is None:
        pil.new("1", (width, height), 1).save(buf, format="PNG", bits=1)
        return buf.getvalue()
    mask = mask.crop(bbox)
    cw, ch = mask.size
    fit = min((width - 2 * margin) / cw, (height - 2 * margin) / ch)
    nw = max(1, int(cw * fit))
    nh = max(1, int(ch * fit))
    small = mask.resize((nw, nh), pil.Resampling.BOX)
    tile = small.point(
        lambda v: 0 if v >= INK_COVERAGE * 255 else 255, mode="1")
    out = pil.new("1", (width, height), 1)
    out.paste(tile, ((width - nw) // 2, (height - nh) // 2))
    out.save(buf, format="PNG", bits=1, optimize=True)
    return buf.getvalue()


def quantize_1bit(values: list[list[int]], threshold: int = 128) -> list[list[int]]:
    """Threshold a grayscale grid (0-255) to 1-bit black/white (0/255).

    Pure function (no imaging libraries) so the pixel fallback stays testable;
    the PNG pipeline in ``tools/calendar_pics.py`` uses the same threshold.
    Values >= threshold become white (255) — ink-free background; values below
    become black (0) — the line work.
    """
    return [
        [255 if v >= threshold else 0 for v in row]
        for row in values
    ]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS cal_pics (
    pic_key    TEXT PRIMARY KEY,
    width      SMALLINT NOT NULL,
    height     SMALLINT NOT NULL,
    mime       TEXT NOT NULL DEFAULT 'image/png',
    bytes      BYTEA NOT NULL,
    tile_1bpp  BYTEA,                   -- 176x176 1-bit row-major image (3872 B),
                                        -- MSB-first, 1 = ink; panel headline pic
    created_ms BIGINT NOT NULL,
    updated_ms BIGINT NOT NULL
)
"""


def _row_to_pic(row: tuple) -> dict[str, Any]:
    pic_key, width, height, mime, png_bytes, tile_1bpp, created_ms, updated_ms = row
    return {
        "pic_key": pic_key,
        "width": width,
        "height": height,
        "mime": mime,
        "bytes": bytes(png_bytes),
        "tile_1bpp": bytes(tile_1bpp) if tile_1bpp is not None else None,
        "created_ms": created_ms,
        "updated_ms": updated_ms,
    }


class PicStore:
    """Postgres-backed picture store keyed by ``CalEvent.pic``.  Thread-safe."""

    DEFAULT_MIME = "image/png"

    def __init__(self, dsn: str) -> None:
        if psycopg is None:
            raise ImportError(
                "psycopg is not installed. Picture store unavailable. "
                "Install with: pip install 'psycopg[binary]'"
            )
        self._lock = threading.Lock()
        self._conn = psycopg.connect(dsn, autocommit=True)
        with self._lock, self._conn.cursor() as cur:
            cur.execute(_SCHEMA)

    def put(self, pic_key: str, png_bytes: bytes, width: int, height: int) -> dict[str, Any]:
        """Upsert a picture by key (idempotent; regenerating a key overwrites).

        Also computes and stores the TILE_WxTILE_H 1-bit RLCD tile when Pillow
        is available (the host skill tooling has it; the server pod only reads).

        Regenerating a tile must reach displays that already show the event:
        clients sync a *delta* keyed by ``cal_events.updated_ms``, so the
        ``lmao_cal_touch_trg`` is exercised against every live event with this
        ``pic`` to bump ``updated_ms``/``rev`` and re-deliver the new tile.
        """
        tile = _png_bytes_to_tile_1bpp(png_bytes)  # None if Pillow absent
        now_ms = _now_ms()
        with self._lock, self._conn.cursor() as cur:
            cur.execute(
                """
                    INSERT INTO cal_pics(
                        pic_key, width, height, mime, bytes, tile_1bpp,
                        created_ms, updated_ms
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(pic_key) DO UPDATE SET
                        width = excluded.width,
                        height = excluded.height,
                        mime = excluded.mime,
                        bytes = excluded.bytes,
                        tile_1bpp = excluded.tile_1bpp,
                        updated_ms = excluded.updated_ms
                    """,
                (pic_key, int(width), int(height), self.DEFAULT_MIME,
                 png_bytes, tile, now_ms, now_ms),
            )
            # Force a delta for every non-tombstoned event that uses this pic,
            # so displays re-fetch the (possibly new-size) tile.  A no-op-value
            # UPDATE still fires the BEFORE UPDATE touch trigger (stamps
            # updated_ms/rev) — see lmao_cal_touch_trg.
            cur.execute(
                "UPDATE cal_events SET title = title"
                " WHERE pic = %s AND deleted = FALSE",
                (pic_key,),
            )
            cur.execute(_GET_SQL, (pic_key,))
            row = cur.fetchone()
        return _row_to_pic(row)

    def get(self, pic_key: str) -> dict[str, Any] | None:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(_GET_SQL, (pic_key,))
            row = cur.fetchone()
        return _row_to_pic(row) if row else None

    def keys(self) -> list[str]:
        with self._lock, self._conn.cursor() as cur:
            cur.execute("SELECT pic_key FROM cal_pics ORDER BY pic_key")
            return [r[0] for r in cur.fetchall()]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_GET_SQL = """
SELECT pic_key, width, height, mime, bytes, tile_1bpp, created_ms, updated_ms
FROM cal_pics WHERE pic_key = %s
"""

# RLCD image contract shared with robodog_client (lma_proto.h) and the server
# (CalEvent.pic_data = field 8).  176x176, row-major, MSB-first, 1 = ink.
# The RoboDog panel shows this (the whole line drawing) only for the first
# ongoing/future event, so the payload stays one image per reply.  Keep
# TILE_W/H in lockstep with lma_proto.h LMA_PB_PIC_TILE and rd_layout.h
# RD_PIC_TILE.
TILE_W = 176
TILE_H = 176
TILE_1BPP_SIZE = (TILE_W * TILE_H + 7) // 8  # 2592


def _png_bytes_to_tile_1bpp(
    png_bytes: bytes, tw: int = TILE_W, th: int = TILE_H,
) -> bytes | None:
    """Downscale a 1-bit-PNG to the RLCD tile: TILE_WxTILE_H row-major 1bpp, MSB
    first, bit 1 = ink (black line work).  Returns None if Pillow is absent
    (the server pod never calls this; the host skill tooling has Pillow)."""
    try:
        import io  # noqa: PLC0415

        from PIL import Image  # noqa: PLC0415
    except ImportError:
        return None
    img = Image.open(io.BytesIO(png_bytes)).convert("L")
    img = img.resize((tw, th), Image.Resampling.NEAREST)
    px = img.load()
    out = bytearray(TILE_1BPP_SIZE)
    for yy in range(th):
        for xx in range(tw):
            if px[xx, yy] < 128:  # black -> ink (bit 1)
                out[(yy * tw + xx) // 8] |= 0x80 >> (xx % 8)
    return bytes(out)


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


def open_pic_store() -> PicStore | None:
    """Open the picture store against the shared in-cluster Postgres.

    Like ``calendar_pg.open_calendar_store``, the DSN comes from the
    contact-book composer (one DB + Secret); returns ``None`` when Postgres is
    not configured or psycopg is missing (calendar pictures then draw no tile).
    """
    dsn = compose_postgres_dsn()
    if not dsn:
        return None
    try:
        return PicStore(dsn)
    except Exception as e:
        _logger.warning("Picture store unavailable (%s) — no calendar pic tiles", e)
        return None
