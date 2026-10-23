"""Unit tests for robodog-client voice statescreen_check pixel geometry.

Synthetic 1-bit frames (pure-Python primitives, no PIL/numpy) drive each
cursor signature: the block / ellipsis / underscore fire on the synthetic
mark, never on a blank or text-only frame, and ``cursor_region_identical``
agrees on identical regions. Runs without the ops venv (pure stdlib), the same
convention as ``//tests:test_e2e_readout``.
"""

import pytest

from robodog_client.voice.statescreen_check import (
    Frame,
    block_signature,
    cursor_region_identical,
    ellipsis_signature,
    underscore_signature,
)


def blank(w, h):
    """All-white (paper) frame."""
    return Frame(w, h, [[True] * w for _ in range(h)])


def set_black(frame, x, y, w=1, h=1):
    for yy in range(y, min(frame.height, y + h)):
        for xx in range(x, min(frame.width, x + w)):
            frame.rows[yy][xx] = False


def text_strokes(frame, y, x0, n=6, stroke_w=3, gap=5):
    """Draw simple vertical-stroke glyphs (simulates a text line, no solids)."""
    x = x0
    for _ in range(n):
        for s in range(stroke_w):
            for yy in range(10):
                set_black(frame, x + s, y + yy, 1, 1)
        x += stroke_w + gap


# -- block cursor ------------------------------------------------------------


def test_block_present():
    f = blank(400, 300)
    set_black(f, 100, 55, 20, 27)  # a 20x27 filled rectangle = block cursor
    ok, box = block_signature(f, 40, 140)
    assert ok is True
    left, top, right, bottom = box
    assert left == 100 and top == 55 and right == 119 and bottom == 81


def test_block_absent_on_blank():
    f = blank(400, 300)
    assert block_signature(f, 40, 140)[0] is False


def test_block_absent_on_text_strokes_only():
    f = blank(400, 300)
    text_strokes(f, 60, 20)
    assert block_signature(f, 40, 140)[0] is False


def test_block_gone_when_pulse_off():
    f = blank(400, 300)
    assert block_signature(f, 40, 140)[0] is False


# -- ellipsis ----------------------------------------------------------------


def _dots(f, y, xs, dot_w=6, dot_h=6):
    for x in xs:
        set_black(f, x, y, dot_w, dot_h)


def test_ellipsis_present():
    f = blank(400, 300)
    _dots(f, 108, [12, 22, 32])  # three 6x6 dots, 4 px gaps
    assert ellipsis_signature(f, 90, 150) is True


def test_ellipsis_absent_on_blank():
    f = blank(400, 300)
    assert ellipsis_signature(f, 90, 150) is False


def test_ellipsis_wrong_spacing():
    f = blank(400, 300)
    _dots(f, 108, [12, 200, 300])  # far apart -> not an ellipsis
    assert ellipsis_signature(f, 90, 150) is False


# -- underscore --------------------------------------------------------------


def test_underscore_present():
    f = blank(400, 300)
    set_black(f, 80, 120, 14, 2)  # a 14x2 horizontal bar
    assert underscore_signature(f, 100, 140) is True


def test_underscore_absent_on_blank():
    f = blank(400, 300)
    assert underscore_signature(f, 100, 140) is False


def test_underscore_not_tripped_by_tall_box():
    f = blank(400, 300)
    set_black(f, 80, 100, 14, 30)  # a tall solid box, not a thin bar
    assert underscore_signature(f, 90, 150) is False


# -- region identity ---------------------------------------------------------


def test_region_identical_matches():
    a = blank(400, 300)
    b = blank(400, 300)
    set_black(a, 80, 120, 14, 2)
    set_black(b, 80, 120, 14, 2)
    assert cursor_region_identical(a, b, 60, 100, 200, 150) is True


def test_region_identical_detects_move():
    a = blank(400, 300)
    b = blank(400, 300)
    set_black(a, 80, 120, 14, 2)
    set_black(b, 90, 120, 14, 2)  # underscore shifted -> not steady
    assert cursor_region_identical(a, b, 60, 100, 200, 150) is False


def test_region_size_mismatch_is_false():
    a = blank(400, 300)
    b = blank(200, 150)  # different panel size
    assert cursor_region_identical(a, b, 0, 0, 10, 10) is False


if __name__ == "__main__":
    import sys

    pytest.main([__file__] + sys.argv[1:])
