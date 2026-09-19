"""Sprout soil-moisture chart for the Cardputer's 240x135 display.

The server appends one machine-readable line to its LXMF reply to every
allow-listed client (see ``lma_core/sprout_history.py``):

    DATA <node8> <dry> <wet> <v0> <v1> ... <vn>

* ``node8``   — first 8 hex chars of the reporting node id (informational)
* ``dry``/``wet`` — the node's active plant-profile band, integer percent;
  ``-1`` means the node has not reported a band yet
* ``v0..vn``  — soil-moisture samples, integer percent, OLDEST FIRST

``parse_data_line()`` and the geometry helpers are pure MicroPython and are
host-tested.  ``draw()`` talks to a display adapter exposing four primitives —
``fill(rgb)``, ``pixel(x, y, rgb)``, ``line(x0, y0, x1, y1, rgb)`` and
``text(s, x, y, rgb)`` — with colours in RGB888; the device's adapter converts
to the panel's colour depth (the Cardputer runs M5Stack's ``M5.Lcd``, which is
LovyanGFX and has no ``st7789``).  ``line`` is optional: without it the trace is
drawn pixel-by-pixel.
"""

WHITE = 0xFFFFFF
GREY = 0x808080
DRY_COLOR = 0xFF0000  # red    — the dry threshold the pump aims above
WET_COLOR = 0x00FFFF  # cyan   — the wet threshold that stops a session
DATA_COLOR = 0xFFFFFF  # white  — the moisture trace

W, H = 240, 135
PLOT_L, PLOT_R = 28, 236
PLOT_T, PLOT_B = 18, 116

_PREFIX = "DATA "


def parse_data_line(text):
    """Extract the first DATA record from *text*, or None.

    Tolerates the ACK text sharing the message (the server sends its ACK line
    first, then this one) and ignores malformed records rather than raising.
    """
    if not text:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith(_PREFIX):
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            node = parts[1]
            dry = int(round(float(parts[2])))
            wet = int(round(float(parts[3])))
            samples = [int(round(float(p))) for p in parts[4:]]
        except (ValueError, TypeError):
            continue
        if not samples:
            continue
        return {"node": node, "dry": dry, "wet": wet, "samples": samples}
    return None


def y_window(dry, wet, samples):
    """Percent range [lo, hi] to draw, always including the band.

    A minimum span keeps a nearly-flat series readable instead of amplifying
    one-point jitter across the whole screen height.
    """
    values = list(samples)
    if dry is not None and dry >= 0:
        values.append(dry)
    if wet is not None and wet >= 0:
        values.append(wet)
    lo = min(values) - 4
    hi = max(values) + 4
    if hi - lo < 20:
        mid = (hi + lo) // 2
        lo, hi = mid - 10, mid + 10
    if lo < 0:
        lo = 0
    if hi > 100:
        hi = 100
    if hi - lo < 4:
        hi = lo + 4
    if hi > 100:
        hi = 100
        lo = hi - 4
    return lo, hi


def y_px(value, lo, hi, top=PLOT_T, bottom=PLOT_B):
    """Map a percentage to a screen row (y grows downward)."""
    span = hi - lo
    if span <= 0:
        return bottom
    v = value - lo
    if v < 0:
        v = 0
    if v > span:
        v = span
    return bottom - (v * (bottom - top)) // span


def x_px(index, count, left=PLOT_L, right=PLOT_R):
    """Map a sample index to a screen column."""
    if count <= 1:
        return left
    return left + (index * (right - left)) // (count - 1)


def _line(tft, x0, y0, x1, y1, color):
    """Draw a line with the display's own primitive, else pixel-by-pixel."""
    line = getattr(tft, "line", None)
    if line is not None:
        line(x0, y0, x1, y1, color)
        return
    # Bresenham: a few hundred pixel() calls per redraw, once a minute.
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    while True:
        tft.pixel(x0, y0, color)
        if x0 == x1 and y0 == y1:
            return
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def draw(tft, data):
    """Render the chart.  Returns a small diagnostics dict.

    Never raises for drawing problems — on failure the error string is
    returned so the caller can log it and the caller's text screen remains
    the fallback.
    """
    result = {"points": 0, "primitive": "pixel", "error": None}
    if tft is None:
        result["error"] = "no display"
        return result
    try:
        samples = list(data.get("samples") or [])
        dry = data.get("dry")
        wet = data.get("wet")

        tft.fill(0x000000)
        latest = f"{samples[-1]}%" if samples else "--"
        tft.text(f"SOIL {latest}", 4, 2, WHITE)
        dry_label = "--" if dry is None or dry < 0 else dry
        wet_label = "--" if wet is None or wet < 0 else wet
        tft.text(f"dry {dry_label}  wet {wet_label}  n={len(samples)}", 4, H - 11, GREY)

        if len(samples) < 2:
            tft.text("waiting for samples", 40, 62, GREY)
            return result

        lo, hi = y_window(dry, wet, samples)

        # Frame + y labels
        _line(tft, PLOT_L, PLOT_T, PLOT_R, PLOT_T, GREY)
        _line(tft, PLOT_L, PLOT_B, PLOT_R, PLOT_B, GREY)
        _line(tft, PLOT_L, PLOT_T, PLOT_L, PLOT_B, GREY)
        _line(tft, PLOT_R, PLOT_T, PLOT_R, PLOT_B, GREY)
        tft.text(str(hi), 2, PLOT_T, GREY)
        tft.text(str(lo), 2, PLOT_B - 8, GREY)

        # Threshold lines, each labelled with its value in its own colour so
        # the boundaries read without cross-referencing the axis.
        if dry is not None and dry >= 0:
            y_dry = y_px(dry, lo, hi)
            _line(tft, PLOT_L + 1, y_dry, PLOT_R - 1, y_dry, DRY_COLOR)
            tft.text(str(dry), PLOT_L + 3, max(PLOT_T, y_dry - 7), DRY_COLOR)
        if wet is not None and wet >= 0:
            y_wet = y_px(wet, lo, hi)
            _line(tft, PLOT_L + 1, y_wet, PLOT_R - 1, y_wet, WET_COLOR)
            tft.text(str(wet), PLOT_L + 3, max(PLOT_T, y_wet - 7), WET_COLOR)

        # Moisture trace
        count = len(samples)
        px = x_px(0, count)
        py = y_px(samples[0], lo, hi)
        tft.pixel(px, py, DATA_COLOR)
        for i in range(1, count):
            x = x_px(i, count)
            y = y_px(samples[i], lo, hi)
            _line(tft, px, py, x, y, DATA_COLOR)
            px, py = x, y
            result["points"] += 1

        # Newest-sample marker (2x2 so it reads as a dot, not a stray pixel),
        # labelled with its value so "now" carries the number too.
        tft.pixel(px, py, WET_COLOR)
        tft.pixel(px - 1, py, WET_COLOR)
        tft.pixel(px, py - 1, WET_COLOR)
        tft.pixel(px - 1, py - 1, WET_COLOR)
        tft.text(str(samples[-1]), max(PLOT_L, px - 16), max(PLOT_T, py - 16), WHITE)
        return result
    except Exception as exc:  # noqa: BLE001 — display drivers vary; never kill the loop
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
