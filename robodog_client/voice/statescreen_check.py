"""Pixel-geometry signatures for the RoboDog hermes 3-state screen (issue #267).

The state-screen behaviour (LISTENING block cursor / PROCESSING ellipsis /
RESPONDING underscore) is firmware-embedded and has NO host C unit test — the
pixel gates are the only accept judge (anti-re-derivation).  This module is the
*mechanical* cross-check for the captured ST7305 frames: it decodes a hermes
RD_SHOT ``.i1`` dump (15000 B -> 400x300 1-bit) with the SAME LUT maths as
``capture.decode_hermes`` but in pure stdlib (no PIL/numpy), so the Bazel unit
tests (``//tests:test_statescreen_check``) run without the ops venv.

The signatures are deliberately geometry-only and vision-independent:
``block_signature`` finds a *solid filled rectangle* of block-cursor
proportions (an LVGL rectangle is a perfect all-black box, unlike glyph
strokes); ``ellipsis_signature`` finds three small filled dots in a row;
``underscore_signature`` finds a thin horizontal bar; ``cursor_region_identical``
compares a region between two frames bit-for-bit (steadiness — the underscore is
constant, never flashing).  Vision readback (``capture.HERMES_CURSOR_PROMPT``)
carries the *text-content* judgement; these signatures carry the mechanical one.

Frame geometry the E2E relies on (see robodog_statescreen.cc):
  root canvas at y=54, line 1 at y=56..92, line 2 at y~=100..136 (400x300).
  Bands passed by the caller are therefore just generous row ranges.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

# Decode constants (must match capture.decode_hermes / custom_lcd_display.cc).
HERMES_W = 400
HERMES_H = 300
HERMES_BUF_SIZE = HERMES_W * HERMES_H // 8  # 15000

# A solid rectangle is the block cursor if its width is roughly one character
# cell of the 30 px text font and it is at least half a line tall but not a
# thin line (fills a cell). Tuned on synthetic frames; E2E records both this
# and vision per frame.
BLOCK_W_MIN = 10
BLOCK_W_MAX = 34
BLOCK_H_MIN = 12
# The block (a filled char cell) is at least 60% as tall as it is wide.
BLOCK_BOXY_FRAC = 0.6

DOT_W_MIN = 2
DOT_W_MAX = 12
DOT_GAP_MIN = 1
DOT_GAP_MAX = 12

UNDERSCORE_W_MIN = 6
UNDERSCORE_H_MAX = 3


class Frame:
    """A decoded hermes panel frame: ``rows[y][x] == True`` means white paper."""

    __slots__ = ("width", "height", "rows")

    def __init__(self, width: int, height: int, rows: Sequence[Sequence[bool]]):
        self.width = width
        self.height = height
        self.rows = rows

    def black(self, x: int, y: int) -> bool:
        if 0 <= y < self.height and 0 <= x < self.width:
            return not bool(self.rows[y][x])
        return False

    def region(self, x0: int, y0: int, x1: int, y1: int) -> list[list[bool]]:
        """(x0,y0)-(x1,y1) inclusive, clipped; True = black pixel."""
        out = []
        for y in range(max(0, y0), min(self.height, y1 + 1)):
            row = []
            for x in range(max(0, x0), min(self.width, x1 + 1)):
                row.append(not bool(self.rows[y][x]))
            out.append(row)
        return out


def decode_frame(data: bytes) -> Frame:
    """15000-byte hermes DispBuffer -> logical 400x300 1-bit frame.

    Inverse of InitLandscapeLUT() in custom_lcd_display.cc: bit==1 is white
    paper. Pure-stdlib port of capture.decode_hermes' pixel maths.
    """
    if len(data) != HERMES_BUF_SIZE:
        raise ValueError(f"hermes frame must be {HERMES_BUF_SIZE} bytes, got {len(data)}")
    buf = data
    h4 = HERMES_H >> 2
    rows: list[list[bool]] = []
    for y in range(HERMES_H):
        inv_y = HERMES_H - 1 - y
        block_y = inv_y >> 2
        local_y = inv_y & 3
        row: list[bool] = []
        for x in range(HERMES_W):
            index = (x >> 1) * h4 + block_y
            bit = 7 - ((local_y << 1) | (x & 1))
            row.append(bool((buf[index] >> bit) & 1))
        rows.append(row)
    return Frame(HERMES_W, HERMES_H, rows)


def _max_solid_rect(frame: Frame, x0: int, y0: int, x1: int, y1: int) -> tuple[int, int, int, int]:
    """Largest all-black axis-aligned rectangle in the given band (inclusive).

    Classic histogram / maximal-rectangle scan. Returns
    ``(left, top, right, bottom)`` inclusive, or ``(0, 0, -1, -1)`` when the
    band has no black pixels. The block cursor — an LVGL filled rectangle — is
    exactly such an all-black box, which glyph strokes rarely produce.
    """
    if y1 < y0 or x1 < x0:
        return (0, 0, -1, -1)
    width = x1 - x0 + 1
    heights = [0] * width
    best = (0, 0, -1, -1)
    best_area = 0
    heights_list = heights
    for y in range(max(0, y0), min(frame.height, y1 + 1)):
        for col in range(width):
            x = x0 + col
            if frame.black(x, y):
                heights_list[col] += 1
            else:
                heights_list[col] = 0
        # Find the largest rectangle whose bottom side is this row.
        stack: list[tuple[int, int]] = []  # (col, height)
        for col in range(width + 1):
            h = heights_list[col] if col < width else 0
            start = col
            while stack and stack[-1][1] > h:
                pc, ph = stack.pop()
                start = pc
                area = ph * (col - pc)
                if area > best_area:
                    best_area = area
                    best = (x0 + pc, y - ph + 1, x0 + col - 1, y)
            if h > 0 and (not stack or stack[-1][1] < h):
                stack.append((start, h))
        # Drain any remaining bars whose rectangle runs to the band's edge.
        for pc, ph in stack:
            area = ph * (width - pc)
            if area > best_area:
                best_area = area
                best = (x0 + pc, y - ph + 1, x0 + width - 1, y)
    return best


def block_signature(frame: Frame, y0: int, y1: int, x0: int = 0, x1: int = HERMES_W - 1):
    """Does the band contain a block-cursor rectangle (solid filled box)?

    Returns ``(True, (left, top, right, bottom))`` or ``(False, None)``. The
    block is an LVGL filled rectangle ~1 char cell wide and >= half a line
    tall; the largest solid box in the band must match those proportions and
    be mostly-filled (a rectangle of that size cannot hide in a glyph).
    """
    left, top, right, bottom = _max_solid_rect(frame, x0, y0, x1, y1)
    if left > right or top > bottom:
        return (False, None)
    w = right - left + 1
    h = bottom - top + 1
    if w < BLOCK_W_MIN or w > BLOCK_W_MAX:
        return (False, None)
    if h < BLOCK_H_MIN:
        return (False, None)
    # The box must be taller than wide (a filled char cell) — a thin horizontal
    # rule or a text underline won't trip it.
    if h < w * BLOCK_BOXY_FRAC:
        return (False, None)
    # Verify the box is SOLID (fully black) — glyph interiors are not.
    solid = True
    for yy in range(top, bottom + 1):
        for xx in range(left, right + 1):
            if not frame.black(xx, yy):
                solid = False
                break
        if not solid:
            break
    if not solid:
        return (False, None)
    return (True, (left, top, right, bottom))


def _runs_in_row(frame: Frame, y: int, x0: int, x1: int) -> list[tuple[int, int]]:
    """Black runs ``(start, end_inclusive)`` on one row within [x0,x1]."""
    runs: list[tuple[int, int]] = []
    cur: int | None = None
    for x in range(x0, x1 + 1):
        if frame.black(x, y):
            if cur is None:
                cur = x
        else:
            if cur is not None:
                runs.append((cur, x - 1))
                cur = None
    if cur is not None:
        runs.append((cur, x1))
    return runs


def ellipsis_signature(frame: Frame, y0: int, y1: int, x0: int = 0, x1: int = 160):
    """Does the band's left area show three small filled dots in a row?

    The PROCESSING "..." is three ASCII dots ~1 cell wide spaced one cell
    apart. Returns True when a row in the band has exactly three black runs of
    dot-like width separated by dot-like gaps (a robust proxy; the E2E pairs
    this with the vision readback).
    """
    for y in range(max(0, y0), min(frame.height, y1 + 1)):
        runs = _runs_in_row(frame, y, x0, min(x1, frame.width - 1))
        if len(runs) < 3:
            continue
        # try every 3 consecutive runs
        for i in range(len(runs) - 2):
            a0, a1 = runs[i]
            b0, b1 = runs[i + 1]
            c0, c1 = runs[i + 2]
            wa, wb, wc = a1 - a0 + 1, b1 - b0 + 1, c1 - c0 + 1
            ga, gb = b0 - a1 - 1, c0 - b1 - 1
            if (
                DOT_W_MIN <= wa <= DOT_W_MAX
                and DOT_W_MIN <= wb <= DOT_W_MAX
                and DOT_W_MIN <= wc <= DOT_W_MAX
                and DOT_GAP_MIN <= ga <= DOT_GAP_MAX
                and DOT_GAP_MIN <= gb <= DOT_GAP_MAX
            ):
                return True
    return False


def underscore_signature(
    frame: Frame, y0: int, y1: int, x0: int = 0, x1: int = HERMES_W - 1
) -> bool:
    return underscore_bbox(frame, y0, y1, x0, x1) is not None


def underscore_bbox(frame: Frame, y0: int, y1: int, x0: int = 0, x1: int = HERMES_W - 1):
    """Bounding box ``(left, top, right, bottom)`` of the RESPONDING ``_`` bar,
    or None. Thin horizontal run with whitespace below (see underscore_signature).
    """
    x1 = min(frame.width - 1, x1)
    below_window = 2
    for y in range(max(0, y0), min(frame.height, y1 + 1)):
        for s, e in _runs_in_row(frame, y, max(0, x0), x1):
            if e - s + 1 < UNDERSCORE_W_MIN:
                continue
            below_blank = 0
            for xx in range(s, e + 1):
                has_below = any(
                    frame.black(xx, yy)
                    for yy in range(y + 1, min(frame.height, y + 1 + below_window))
                )
                if not has_below:
                    below_blank += 1
            if below_blank >= (e - s + 1) // 2:
                return (s, y, e, y)
    return None


def cursor_region_identical(a: Frame, b: Frame, x0: int, y0: int, x1: int, y1: int) -> bool:
    """Bit-for-bit equality of [x0..x1]x[y0..y1] between two frames.

    Used for RESPONDING steadiness: the ``_`` is constant (non-flashing), so
    the region around it is pixel-identical across two frames that both show
    the fully-revealed reply. False when a region is out of bounds or differs.
    """
    if a.width != b.width or a.height != b.height:
        return False
    for y in range(max(0, y0), min(a.height, y1 + 1)):
        for x in range(max(0, x0), min(a.width, x1 + 1)):
            if a.black(x, y) != b.black(x, y):
                return False
    return True


def describe_band(frame: Frame, y0: int, y1: int) -> str:
    """Human-digestible dump of the signatures for one band (E2E evidence)."""
    block = block_signature(frame, y0, y1)
    ell = ellipsis_signature(frame, y0, y1)
    und = underscore_signature(frame, y0, y1)
    return f"block={block}, ellipsis={ell}, underscore={und}"


if __name__ == "__main__":  # pragma: no cover - diagnostic entrypoint
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("i1", help="hermes .i1 frame dump (15000 bytes)")
    ap.add_argument("--y0", type=int, default=40)
    ap.add_argument("--y1", type=int, default=140)
    args = ap.parse_args()
    with open(args.i1, "rb") as fh:
        frame = decode_frame(fh.read())
    print(describe_band(frame, args.y0, args.y1))
    sys.exit(0)
