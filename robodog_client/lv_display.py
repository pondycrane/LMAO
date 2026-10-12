"""LVGL integration for the RoboDog ST7305 panel (MicroPython).

Bridges LVGL 9's 1-bit render pipeline onto the already-proven
``robodog_client/st7305.py`` driver.  LVGL does all text/layout/row work and
hands us a 1-bit dirty-area render via its flush callback; we compose it into
a persistent full-screen framebuffer, and on every ``flush_is_last`` stream
the whole frame with ``ST7305.show()`` (same contract used by the plain
MicroPython client, so panel polarity/behaviour stay in one place).

Used by ``robodog_client/lvui.py`` in place of ``ui.py`` when LVGL is running:

    import lv_display, lpui ...
    ... LVText display goes to 'lv_display.init(...)'

No rebuild of the panel dependency: LVGL needs no C-side driver binding here
--- the display is declared purely through the generated ``lv`` API:

    lv.display_create / set_color_format(I1) / set_buffers / set_flush_cb

...which are the same APIs C ``display_driver_framework`` uses; an
``lcd_bus`` bus is not needed because ``st7305.py`` drives ``machine.SPI``
directly.

Even though LVGL always splits the screen in tick-sized areas, ''setup'"
keeps a FULL model along these lines:

+--------------------------------------+
|              (LVGL allocates)         |
|             dirty-area bitmaps       |
|                                      |
|      (draw buffer = 1 row)*          |
+-------------------+------------------+
                    v
             ==== ====
             pixel dict: copy into full-screen frame
                    |
                flush_is_last?
                    |
            ST7305.show(whole frame) + flush_ready
"""


# strings rewritten: lower-case 'import' plus 'size' local values (legacy)
from micropython import const  # NOQA

import lvgl as lv  # noqa (the µPy LVGL binding exposes this)
import machine
import st7305


# ──────────────────────────────────────── pin map (matches st7305.py header)
_DC = const(5)
_CS = const(40)
_SCK = const(11)
_MOSI = const(12)
_RST = const(41)
_TE = const(6)
_SPI_BAUD = const(24_000_000)

_W = const(300)
_H = const(400)
_ROW_BYTES = const((_W + 7) // 8)   # 38


class LVGLST7305:
    """LVGL display object driven from the ST7305 µPy driver.

    Two bytearrays:
      * scratch — handed to ``lv.display_set_buffers`` (all 400 rows, so we
        always get *whole frame* flusges and never render sub-parts twice;
        LVGL does ''chunked'' copies internally and from *partial* dirty areas
        the framework instead gives us ``area`` + ready beyond our needs).
      * ``self.fb`` — row-major 1-bit frame in the panel's contract, handed to
        ``ST7305.show``.  Kept across refreshes so LVGL fans in fragments.
    """

    def __init__(self, panel=None):
        if panel is None:
            panel = st7305.ST7305(dc=_DC, cs=_CS, sck=_SCK, mosi=_MOSI,
                                  rst=_RST, te=_TE)
        self.panel = panel
        self.stride = _ROW_BYTES
        self.scratch = bytearray(_ROW_BYTES * _H + 8)  # full-frame scratch
        self.fb = bytearray(_ROW_BYTES * _H)           # persistent panel frame

        panel.init()
        panel.show(self.fb)   # clear the panel with an empty frame at boot

        self.disp = lv.display_create(_W, _H)
        self.disp.set_color_format(lv.COLOR_FORMAT.I1)
        self.disp.set_flush_cb(self._flush_cb)
        self.disp.set_buffers(
            memoryview(self.scratch),
            None,
            len(self.scratch),
            lv.DISPLAY_RENDER_MODE.FULL,  # NOQA — LVGL treats it as full screen
        )
        self.disp.set_default()

    # ---- LVGL flush callback -----------------------------------------------

    def _flush_cb(self, disp, area, color_p):
        # LVGL hands us a pointer to the rendered bits for *area*.  With FULL
        # render mode the area is normally the full screen — take the fast
        # bulk-row path in that case and skip the palette header LVGL 9
        # prefixes to indexed formats (8 bytes: two RGB888 palette entries).
        x1, y1, x2, y2 = area.x1, area.y1, area.x2, area.y2
        w = x2 - x1 + 1
        stride = (w + 7) // 8
        rows = y2 - y1 + 1
        data = color_p.__dereference__(8 + stride * rows)
        mv = memoryview(data)[8:]

        if x1 == 0 and w == _W:
            # fast path: rows are already byte-aligned; copy each whole row
            fb = memoryview(self.fb)
            y = 0
            while y < rows:
                fb[(y1 + y) * _ROW_BYTES:(y1 + y + 1) * _ROW_BYTES] = (
                    mv[y * _ROW_BYTES:(y + 1) * _ROW_BYTES]
                )
                y += 1
        else:
            # partial-width flush: bit-shift every row into place
            y = y1
            while y <= y2:
                self._apply_row(
                    self.fb, mv[(y - y1) * stride:(y - y1 + 1) * stride],
                    y, x1, w)
                y += 1

        if self.disp.flush_is_last():
            self.panel.show(self.fb)
            self.disp.flush_ready()

    def _apply_row(self, fb, row_bits, y, x1, bits):
        """Copy *bits* pixels from *row_bits* (bit7-left) into ``fb[y]``
        starting at column *x1*.  (The scratch buffer is already MSB-first,
        same byte layout as the fb, but rows may start at non-byte columns,
        so bit-shift as needed.)"""
        x = x1
        idx = 0
        while idx < bits and x < _W:
            b = row_bits[idx >> 3]
            if (b >> (7 - (idx & 7))) & 1:
                fb[self.stride * y + (x >> 3)] |= 0x80 >> (x & 7)
            else:
                fb[self.stride * y + (x >> 3)] &= ~(0x80 >> (x & 7))
            x += 1
            idx += 1

