"""MicroPython driver for the Waveshare ESP32-S3 RLCD-4.2 (ST7305, 300x400).

1-bit monochrome reflective LCD driver.  The panel is an ST7305 controller over
SPI; this is a faithful MicroPython port of Waveshare's ESP-IDF U8G2 driver
(`u8g2_st7305.c`) — the full init sequence table and the 4x4-nibble LUT tile
write are reproduced verbatim so the on-panel data ordering matches.

Buffer contract (used by `robodog_client/ui.py` and `ST7305.show`): a
``bytearray(ROW_BYTES * HEIGHT)`` in **row-major** layout — pixel row ``y`` is
bytes ``[y*ROW_BYTES, (y+1)*ROW_BYTES)``, and within a byte bit 7 is the
leftmost pixel (x increases with bit index 7..0).  ``show()`` re-maps this
into the panel's native 8x8-tile column format and streams it via the windowed
``0x2C`` memory write.

.. note:: HARDWARE-UNVERIFIED — this driver is a best-effort port, not yet
   validated on a live panel (no board available at authoring time).  The pin
   map and init table come from Waveshare's official ESP-IDF example; if
   colours/layout are inverted on the real panel, flip the polarity in
   :meth:`_frame_to_panel` and confirm the window-column ordering.
"""

from machine import Pin, SoftSPI


class ST7305:
    """300x400 1-bit ST7305 reflective LCD over SPI."""

    WIDTH = 300
    HEIGHT = 400
    ROW_BYTES = (WIDTH + 7) // 8  # 38
    _TILES_Y = HEIGHT // 8        # 50 eight-row bands
    _TILES_X = 38                 # 38 eight-column tiles per band

    # Navigation + buttons pins from the board schematic; SPI data lines from
    # the Waveshare u8g2_st7305 example user_config.h.
    # DC(PIN5) CS(PIN40) SCK(PIN11) MOSI(PIN12) RST(PIN41) TE(PIN6)
    def __init__(self, dc=5, cs=40, sck=11, mosi=12, rst=41, te=6):
        self._sck = Pin(sck, Pin.OUT)
        self._mosi = Pin(mosi, Pin.OUT)
        self._dc = Pin(dc, Pin.OUT)
        self._cs = Pin(cs, Pin.OUT)
        self._rst = Pin(rst, Pin.OUT)
        self._spi = SoftSPI(
            baudrate=24_000_000, polarity=0, phase=0,
            sck=self._sck, mosi=self._mosi, miso=None,
        )
        self._spi.init(baudrate=24_000_000, firstbit=SoftSPI.MSB)

    # ---- low-level -----------------------------------------------------------
    def _cmd(self, value):
        self._cs.value(0)
        self._dc.value(0)  # command
        self._spi.write(bytes([value]))
        self._cs.value(1)

    def _cmd_data(self, cmd, data):
        self._cs.value(0)
        self._dc.value(0)
        self._spi.write(bytes([cmd]))
        self._dc.value(1)  # parameter
        self._spi.write(bytes(data))
        self._cs.value(1)

    def _data(self, buf):
        self._cs.value(0)
        self._dc.value(1)
        self._spi.write(bytes(buf))
        self._cs.value(1)

    def reset(self):
        self._rst.value(1)
        time_ms(50)
        self._rst.value(0)
        time_ms(20)
        self._rst.value(1)
        time_ms(50)

    # ---- init (Waveshare u8g2_st7305.c st7305_full_init, verbatim) ----------
    def init(self):
        self.reset()
        table = [
            (0xD6, (0x17, 0x02)),
            (0xD1, (0x01,)),
            (0xC0, (0x11, 0x04)),
            (0xC1, (0x69, 0x69, 0x69, 0x69)),
            (0xC2, (0x19, 0x19, 0x19, 0x19)),
            (0xC4, (0x4B, 0x4B, 0x4B, 0x4B)),
            (0xC5, (0x19, 0x19, 0x19, 0x19)),
            (0xD8, (0x80, 0xE9)),
            (0xB2, (0x02,)),
            (0xB3, (0xE5, 0xF6, 0x05, 0x46, 0x77, 0x77, 0x77, 0x77, 0x76, 0x45)),
            (0xB4, (0x05, 0x46, 0x77, 0x77, 0x77, 0x77, 0x76, 0x45)),
            (0x62, (0x32, 0x03, 0x1F)),
            (0xB7, (0x13,)),
            (0xB0, (0x64,)),
        ]
        for cmd, data in table:
            self._cmd_data(cmd, data)
        self._cmd(0x11)  # sleep out
        time_ms(120)
        self._cmd_data(0xC9, (0x00,))
        self._cmd_data(0x36, (0x48,))
        self._cmd_data(0x3A, (0x11,))
        self._cmd_data(0xB9, (0x20,))
        self._cmd_data(0xB8, (0x29,))
        self._cmd(0x21)  # invoff
        self._cmd_data(0x2A, (0x12, 0x2A))  # column window
        self._cmd_data(0x2B, (0x00, 0xC7))  # row window
        self._cmd_data(0x35, (0x00,))
        self._cmd_data(0xD0, (0xFF,))
        self._cmd(0x38)  # idle off
        self._cmd(0x29)  # display on

    # ---- full-frame refresh --------------------------------------------------
    def show(self, frame):
        """Blit a row-major 1-bit frame (ROW_BYTES * HEIGHT bytes) to the panel.

        Re-maps each 8-row band into the ST7305's 8x8-tile column format and
        streams it via the windowed memory write (port of the U8G2 tile draw).
        ``frame`` may be None → keep the last image (RLCD holds the frame when
        powered down).
        """
        if frame is None:
            return
        assert len(frame) >= self.ROW_BYTES * self.HEIGHT, "frame too small"
        for y_band in range(self._TILES_Y):
            self._draw_band(frame, y_band, 0, self._TILES_X)

    # The 4x4 LUT from u8g2_st7305.c: maps 2-bit pixel taps to panel nibbles.
    _LUT = (
        (0x00, 0x80, 0x40, 0xC0),
        (0x00, 0x20, 0x10, 0x30),
        (0x00, 0x08, 0x04, 0x0C),
        (0x00, 0x02, 0x01, 0x03),
    )

    def _column_bytes(self, frame, y_band, col, cols):
        """Build U8G2-style column bytes (8 vertical pixels each) for a band's
        pixel-column range [col, col+cols) from the row-major frame."""
        out = bytearray(cols)
        base_row = y_band * 8
        rb = self.ROW_BYTES
        for i in range(cols):
            c = col + i
            b = c >> 3
            bit = 7 - (c & 7)  # MSB = leftmost
            v = 0
            for r in range(8):
                if frame[(base_row + r) * rb + b] & (1 << bit):
                    v |= (1 << r)  # bit r = row offset in the band
            out[i] = v
        return out

    def _draw_band(self, frame, y_pos, x_pos, cnt):
        """Port of U8G2_MSG_DISPLAY_DRAW_TILE for one 8-row band of `cnt`
        eight-column tiles starting at tile x_pos (8px each).  ``row_base``
        spans the full panel width (300 column bytes), indexed by the absolute
        pixel column exactly as the C driver does."""
        first_col = x_pos * 8
        last_col = (x_pos + cnt) * 8 - 1
        if last_col >= self.WIDTH:
            last_col = self.WIDTH - 1

        addr_start = 0x12 + first_col // 12
        addr_end = 0x12 + last_col // 12
        send_cnt = (addr_end - addr_start + 1) * 3

        addr_first_col = (addr_start - 0x12) * 12
        addr_last_col = (addr_end - 0x12) * 12 + 11
        if addr_last_col >= self.WIDTH:
            addr_last_col = self.WIDTH - 1

        # U8G2-style column bytes for every pixel column (absolute index).
        row_base = self._column_bytes(frame, y_pos, 0, self.WIDTH)

        all_rows = bytearray(send_cnt * 4)
        for sr in range(4):
            shift = sr * 2
            base_off = sr * send_cnt
            for j, col in enumerate(range(addr_first_col, addr_last_col + 1, 4)):
                idx = base_off + j
                all_rows[idx] = (
                    self._LUT[0][(row_base[col] >> shift) & 3]
                    | self._LUT[1][(row_base[col + 1] >> shift) & 3]
                    | self._LUT[2][(row_base[col + 2] >> shift) & 3]
                    | self._LUT[3][(row_base[col + 3] >> shift) & 3]
                )

        col_bounds = (0x3C - addr_end, 0x3C - addr_start)
        row_bounds = (y_pos * 4, y_pos * 4 + 3)
        self._cmd_data(0x2A, col_bounds)
        self._cmd_data(0x2B, row_bounds)
        self._cmd_data(0x2C, all_rows)


def time_ms(ms):
    import time

    time.sleep_ms(ms)
