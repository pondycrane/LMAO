"""Postgres-backed store for family-calendar line-drawing pictures.

Event pictures (``CalEvent.pic`` keys) are small **1-bit black-and-white**
PNGs, generated out-of-band by the ``tools/calendar_pics.py`` skill tooling
(an agent rasterizes an SVG/PNG source at the RLCD tile size, converts it to
black/white, and upserts the bytes here).  This module only *stores and
serves* those bytes — no image processing runs in the server pod.

Storage contract (RLCD display constraints)
-------------------------------------------
- Target display: 4.2" reflective LCD, 300x400 portrait.
- A picture is a content tile, deliberately smaller than the full panel so
  the screen still has room for the title/time/date rows:
  :func:`pick_tile_size` defaults to 144x144 (≈ half the panel width).
- Format: 1-bit PNG (``mode '1'``).  At 144x144 a compact line drawing is a
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
DEFAULT_TILE_W = 144
DEFAULT_TILE_H = 144


def pick_tile_size(screen_w: int = SCREEN_W, screen_h: int = SCREEN_H) -> tuple[int, int]:
    """Return (width, height) for a calendar picture on the 4.2" RLCD panel.

    The picture is a tile inside the event row: it must leave room for the
    title/time/date text.  Defaults to a 144x144 square (half the 300px
    width); smaller of the two dimensions governs so the tile never exceeds
    the panel.
    """
    return (min(DEFAULT_TILE_W, screen_w - 20), min(DEFAULT_TILE_H, screen_h - 20))


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
    created_ms BIGINT NOT NULL,
    updated_ms BIGINT NOT NULL
)
"""


def _row_to_pic(row: tuple) -> dict[str, Any]:
    pic_key, width, height, mime, png_bytes, created_ms, updated_ms = row
    return {
        "pic_key": pic_key,
        "width": width,
        "height": height,
        "mime": mime,
        "bytes": bytes(png_bytes),
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
        """Upsert a picture by key (idempotent; regenerating a key overwrites)."""
        now_ms = _now_ms()
        with self._lock, self._conn.cursor() as cur:
            cur.execute(
                """
                    INSERT INTO cal_pics(
                        pic_key, width, height, mime, bytes, created_ms, updated_ms
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(pic_key) DO UPDATE SET
                        width = excluded.width,
                        height = excluded.height,
                        mime = excluded.mime,
                        bytes = excluded.bytes,
                        updated_ms = excluded.updated_ms
                    """,
                (pic_key, int(width), int(height), self.DEFAULT_MIME,
                 png_bytes, now_ms, now_ms),
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
SELECT pic_key, width, height, mime, bytes, created_ms, updated_ms
FROM cal_pics WHERE pic_key = %s
"""


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
