"""
RoboDog ST7305 calendar renderer — pure MicroPython, host-testable.

Draws the family calendar (header line + up to ``MAX_ROWS`` event rows: title
and HH:MM time, plus an optional 16x16 pic box) as a 1-bit frame for the
Waveshare ESP32-S3 RLCD-4.2 reflective LCD.

Framebuffer / ST7305 contract (``robodog_client/st7305.py`` conforms):
  * The frame is a ``bytearray(ROW_BYTES * HEIGHT)`` with WIDTH=300,
    HEIGHT=400, ROW_BYTES=(WIDTH+7)//8 == 38  (i.e. ``st7305.ST7305.ROW_BYTES``).
  * Row-major: pixel row ``y`` occupies bytes ``[y*ROW_BYTES, (y+1)*ROW_BYTES)``.
  * Each byte packs 8 horizontal pixels: bit 7 is the leftmost pixel and the
    bit index increases with ``x`` (``0x80 >> (x % 8)``); a set bit = ink.
  * ``ST7305.show(frame)`` blits such a frame; ``show(None)`` keeps the last
    image (main.py uses that to persist the frame across a failed redraw).

This module imports no MicroPython hardware modules (``ui.render`` talks to any
object exposing ``show(frame)``, so host tests can pass a fake display).  The
small built-in 8x8 font means the renderer runs identically on device and on
CPython.  Unknown characters (and lowercase, which maps to uppercase) fall back
to a blank cell — titles render in caps, which is deliberate for the 8x8 font.
"""

# ---- Display geometry (MUST match st7305.ST7305) -------------------------
WIDTH = 300
HEIGHT = 400
ROW_BYTES = (WIDTH + 7) // 8  # 38

# ---- Layout --------------------------------------------------------------
CHAR_W = 6        # monospace advance per glyph cell (px)
CHAR_H = 8        # glyph cell height (px)
GLYPH_W = 5       # ink width inside the cell (px, left-aligned)
MAX_ROWS = 6      # max event rows drawn at once ("Show at most ~6 rows")
HEADER_H = 20     # height of the header band; separator line at y=HEADER_H-1
ROW_Y0 = 28       # y of the first event row
ROW_PITCH = 60    # vertical pitch between event rows
ROW_TIME_DY = 32  # time label offset below a row's origin
TEXT_X = 8        # left margin for text
PIC_W = 16        # pic placeholder box size (v1 draws only the box outline)
PIC_H = 16


# Bitmap font -----------------------------------------------------------------
# Each glyph is 7 rows of 5-char strings ('#' = ink, '.' = blank).  The 8th
# cell row is left empty.  Rows are packed left-aligned: row bit 7 = leftmost.
_FONT = {
    " ": (".", ".", ".", ".", ".", ".", "."),
    "0": (".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "1": ("..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "2": (".###.", "#...#", "....#", "..##.", ".#...", "#....", "#####"),
    "3": ("#####", "....#", "...#.", "..#..", "....#", "#...#", ".###."),
    "4": ("...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."),
    "5": ("#####", "#....", "####.", "....#", "....#", "#...#", ".###."),
    "6": ("..##.", ".#...", "#....", "####.", "#...#", "#...#", ".###."),
    "7": ("#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."),
    "8": (".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."),
    "9": (".###.", "#...#", "#...#", ".####", "....#", "...#.", ".##.."),
    "A": (".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "B": ("####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."),
    "C": (".####", "#....", "#....", "#....", "#....", "#....", ".####"),
    "D": ("###..", "#..#.", "#...#", "#...#", "#...#", "#..#.", "###.."),
    "E": ("#####", "#....", "#....", "####.", "#....", "#....", "#####"),
    "F": ("#####", "#....", "#....", "####.", "#....", "#....", "#...."),
    "G": (".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".###."),
    "H": ("#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "I": (".###.", "..#..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "J": ("..###", "...#.", "...#.", "...#.", "...#.", "#...#", ".###."),
    "K": ("#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"),
    "L": ("#....", "#....", "#....", "#....", "#....", "#....", "#####"),
    "M": ("#...#", "##.##", "#.#.#", "#.#.#", "#...#", "#...#", "#...#"),
    "N": ("#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#", "#...#"),
    "O": (".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "P": ("####.", "#...#", "#...#", "####.", "#....", "#....", "#...."),
    "Q": (".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"),
    "R": ("####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"),
    "S": (".####", "#....", "#....", ".###.", "....#", "....#", "####."),
    "T": ("#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."),
    "U": ("#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "V": ("#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."),
    "W": ("#...#", "#...#", "#...#", "#.#.#", "#.#.#", "##.##", "#...#"),
    "X": ("#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"),
    "Y": ("#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."),
    "Z": ("#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"),
    "-": (".", ".", ".", "#####", ".", ".", "."),
    ":": (".", "..#..", "..#..", ".", "..#..", "..#..", "."),
    "/": ("....#", "....#", "...#.", "..#..", ".#...", "#....", "#...."),
    ".": (".", ".", ".", ".", ".", "..#..", "..#.."),
    ",": (".", ".", ".", ".", "..#..", "..#..", "#...."),
    "'": ("..#..", "..#..", "..#..", ".", ".", ".", "."),
    "!": ("..#..", "..#..", "..#..", "..#..", "..#..", ".", "..#.."),
    "?": (".###.", "#...#", "....#", "...#.", "..#..", ".", "..#.."),
    "&": (".###.", "#...#", "#.#..", ".#...", "#.#.#", "#...#", ".##.#"),
    "(": ("..#..", ".#...", "#....", "#....", "#....", ".#...", "..#.."),
    ")": ("..#..", "...#.", "....#", "....#", "....#", "...#.", "..#.."),
    "+": (".", ".", "..#..", ".#####", "..#..", ".", "."),
    "@": (".###.", "#...#", "##..#", "#.#.#", "#.###", "#....", ".###."),
    "#": (".#.#.", ".#.#.", "#####", ".#.#.", "#####", ".#.#.", ".#.#."),
    "%": ("#...#", "#...#", "...#.", "..#..", ".#...", "#...#", "#...#"),
    "*": (".", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "."),
    "=": (".", ".", "#####", ".", "#####", ".", "."),
    '"': (".#.#.", ".#.#.", ".#.#.", ".", ".", ".", "."),
    "<": ("...#.", "..#..", ".#...", "#....", ".#...", "..#..", "...#."),
    ">": (".#...", "..#..", "...#.", "....#", "...#.", "..#..", ".#..."),
}


def _pack_row(pattern):
    """Pack a 5-char row into the high bits of a byte (bit 7 = leftmost)."""
    b = 0
    for i, ch in enumerate(pattern):
        if ch == "#":
            b |= 0x80 >> i
    return b


def _glyph_pattern(ch):
    pat = _FONT.get(ch)
    if pat is None and isinstance(ch, str):
        pat = _FONT.get(ch.upper())  # lowercase -> uppercase for the 8x8 font
    if pat is None:
        pat = _FONT[" "]
    return pat


def glyph(ch):
    """Return the 8-byte bitmap for one 8x8 glyph cell (bit 7 = left pixel).

    Exposed so tests can reconstruct exactly the bytes ``render_into`` places.
    """
    return bytes([_pack_row(row) for row in _glyph_pattern(ch)]) + b"\x00"


# Primitive drawing (1-bit, MSB-first per byte) ------------------------------


def _clear(buf):
    for i in range(len(buf)):
        buf[i] = 0


def _set_pixel(buf, x, y):
    if 0 <= x < WIDTH and 0 <= y < HEIGHT:
        buf[y * ROW_BYTES + (x >> 3)] |= 0x80 >> (x & 7)


def _hline(buf, x0, x1, y):
    for x in range(x0, x1 + 1):
        _set_pixel(buf, x, y)


def _vline(buf, x, y0, y1):
    for y in range(y0, y1 + 1):
        _set_pixel(buf, x, y)


def _box(buf, x, y, w, h):
    _hline(buf, x, x + w - 1, y)
    _hline(buf, x, x + w - 1, y + h - 1)
    _vline(buf, x, y, y + h - 1)
    _vline(buf, x + w - 1, y, y + h - 1)


def text_width(text):
    """Width (px) of *text* at the fixed CHAR_W advance."""
    return len(text) * CHAR_W


def draw_text(buf, text, x, y):
    """Draw *text* at (x, y) using the 8x8 glyph cells.  Returns the x
    position just past the last glyph (for layout tests)."""
    if not isinstance(text, str):
        text = str(text)
    start_x = x
    for ch in text:
        g = glyph(ch)
        for r in range(8):
            row_byte = g[r]
            if not row_byte:
                continue
            yy = y + r
            if not 0 <= yy < HEIGHT:
                continue
            base = yy * ROW_BYTES
            for k in range(8):
                if row_byte & (0x80 >> k):
                    xx = x + k
                    if 0 <= xx < WIDTH:
                        buf[base + (xx >> 3)] |= 0x80 >> (xx & 7)
        x += CHAR_W
    return x


# Time / header formatting ---------------------------------------------------


def _fmt_hhmm(ms):
    if not ms:
        return ""
    import time as _time

    try:
        lt = _time.localtime(ms // 1000)
        return "%02d:%02d" % (lt[3], lt[4])
    except Exception:
        return ""


def _default_header(now_ms):
    if not now_ms:
        return "FAMILY CALENDAR"
    import time as _time

    try:
        lt = _time.localtime(now_ms // 1000)
        return "%04d-%02d-%02d  %02d:%02d" % (lt[0], lt[1], lt[2], lt[3], lt[4])
    except Exception:
        return "FAMILY CALENDAR"


# Event selection ------------------------------------------------------------


def _select_events(events_list, now_ms=None, max_rows=MAX_ROWS):
    """Drop deleted / past / title-less events, sort by start_ms, cap rows."""
    kept = []
    for ev in events_list or []:
        if not isinstance(ev, dict):
            continue
        if ev.get("deleted"):
            continue
        if not (ev.get("title") or "").strip():
            continue
        end_ms = ev.get("end_ms") or 0
        if now_ms and end_ms and end_ms < now_ms:
            continue  # event is already over
        kept.append(ev)
    kept.sort(key=lambda e: e.get("start_ms") or 0)
    if max_rows is not None:
        kept = kept[:max_rows]
    return kept


# Public API -----------------------------------------------------------------


def render_into(buf, events_list, now_ms=None, header_text=None,
                max_rows=MAX_ROWS, offline=False):
    """Pure, host-testable renderer — no display, no hardware.

    Draws the calendar frame (header + event rows) into *buf*, which must be a
    ``bytearray(ROW_BYTES * HEIGHT)`` (38*400 == 15200 bytes) 1-bit framebuffer
    per the ST7305 contract above.  The buffer is cleared first.

    Args:
        buf: bytearray(ROW_BYTES*HEIGHT) to draw into (cleared before use).
        events_list: list of CalEvent dicts (CalendarBundle["events"]):
            {start_ms, end_ms, title, notes, deleted, pic, ...}.
        now_ms: current epoch-ms; past events (end_ms < now_ms) are skipped and
            (when *header_text* is None) the header is a date/time line.
        header_text: explicit header line; overrides the now_ms-derived one.
        max_rows: max event rows to draw (default MAX_ROWS=6).
        offline: prefix "OFFLINE " to the header line (cached/SD render).

    Returns a list (one entry per drawn row) of
    ``{"title", "time_label", "y", "pic"}`` for tests/main.py inspection.
    """
    if buf is None or len(buf) != ROW_BYTES * HEIGHT:
        raise ValueError("buf must be a bytearray of %d bytes" % (ROW_BYTES * HEIGHT))

    _clear(buf)

    # ---- header band ----
    if header_text is None:
        header_text = _default_header(now_ms)
    if offline:
        header_text = "OFFLINE " + header_text
    draw_text(buf, header_text, TEXT_X, 4)
    _hline(buf, 0, WIDTH - 1, HEADER_H - 1)  # separator under the header

    # ---- event rows ----
    events = _select_events(events_list, now_ms=now_ms, max_rows=max_rows)
    shown = []
    for i, ev in enumerate(events):
        y = ROW_Y0 + i * ROW_PITCH
        title = (ev.get("title") or "").strip()
        time_label = _fmt_hhmm(ev.get("start_ms") or 0)
        pic = bool(ev.get("pic"))

        if pic:  # v1: 16x16 box placeholder; artwork drawn by a later version
            _box(buf, WIDTH - 8 - PIC_W, y + 4, PIC_W, PIC_H)

        tex_right = (WIDTH - 8 - PIC_W - 12) if pic else (WIDTH - 8)
        max_chars = max(int((tex_right - TEXT_X) // CHAR_W), 1)
        title = title[:max_chars]
        draw_text(buf, title, TEXT_X, y + 4)
        if time_label:
            draw_text(buf, time_label, TEXT_X, y + ROW_TIME_DY)

        shown.append({"title": title, "time_label": time_label, "y": y, "pic": pic})

    return shown


def render(display, events_list, now_ms=None, header_text=None,
           max_rows=MAX_ROWS, offline=False):
    """Thin wrapper: allocate the 38*400 framebuffer, ``render_into`` it, then
    blit via ``display.show(buf)``.

    *display* must expose ``show(frame)`` (see the ST7305 contract above);
    any object with that method works (host tests pass a fake).  ``show``
    failures are logged, never fatal.

    Returns the framebuffer bytes that were drawn (main.py can later call
    ``display.show(None)`` to keep the last image across a failed redraw).
    """
    buf = bytearray(ROW_BYTES * HEIGHT)
    render_into(buf, events_list, now_ms=now_ms, header_text=header_text,
                max_rows=max_rows, offline=offline)
    if display is not None:
        try:
            display.show(buf)
        except Exception as e:
            print("ui.render: display.show failed: %s" % e)
    return buf
