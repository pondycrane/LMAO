"""Sprout chart for the Cardputer's 240x135 display: soil moisture, air
humidity and air temperature.

The server appends one machine-readable line to its LXMF reply to every
allow-listed client (see ``lma_core/sprout_history.py``):

    DATA <node8> <dry> <wet> <ct> <t0> ... <tt> <ch> <h0> ... <hh> <cm> <m0> ... <mm>

* ``node8``   — first 8 hex chars of the reporting node id (informational)
* ``dry``/``wet`` — the node's active plant-profile band, integer percent;
  ``-1`` means the node has not reported a band yet
* three count-prefixed series — air temperature in °C, air humidity in %,
  then soil moisture in % — each OLDEST FIRST.  Lengths may differ (e.g. a
  report with a failed air sensor only grows the moisture series), so each
  series carries its own count.

``parse_data_line()`` and the geometry helpers are pure MicroPython and are
host-tested.  ``draw()`` talks to a display adapter exposing four primitives —
``fill(rgb)``, ``pixel(x, y, rgb)``, ``line(x0, y0, x1, y1, rgb)`` and
``text(s, x, y, rgb)`` — with colours in RGB888; the device's adapter converts
to the panel's colour depth (the Cardputer runs M5Stack's ``M5.Lcd``, which is
LovyanGFX and has no ``st7789``).  ``line`` is optional: without it traces are
drawn pixel-by-pixel.
"""

WHITE = 0xFFFFFF
GREY = 0x808080
DRY_COLOR = 0xFF0000  # red    — the soil dry threshold the pump aims above
WET_COLOR = 0x00FFFF  # cyan   — the soil wet threshold that stops a session
SOIL_COLOR = 0xFFFFFF  # white  — the soil-moisture trace
HUM_COLOR = 0x00FF00  # green  — the air-humidity trace (same % axis as soil)
TEMP_COLOR = 0xFFA500  # orange — the air-temperature trace (own right °C axis)

W, H = 240, 135
# Plot box; the right gutter (PLOT_R..240) holds the temperature axis labels.
PLOT_L, PLOT_R = 28, 210
PLOT_T, PLOT_B = 18, 116
_TEMP_AXIS_X = PLOT_R + 6

_PREFIX = "DATA "


def parse_data_line(text):
    """Extract the first DATA record from *text*, or None.

    Tolerates the ACK text sharing the message (the server sends its ACK line
    first, then this one) and ignores malformed records rather than raising.
    A line in the old moisture-only format no longer parses (it is ignored,
    not misdrawn) — the server and the Cardputer are flashed together.
    """
    if not text:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith(_PREFIX):
            continue
        parts = line.split()
        try:
            node = parts[1]
            dry = int(round(float(parts[2])))
            wet = int(round(float(parts[3])))
            idx = 4
            ct = int(parts[idx])
            idx += 1
            temp = []
            for _ in range(ct):
                temp.append(float(parts[idx]))
                idx += 1
            ch = int(parts[idx])
            idx += 1
            humidity = []
            for _ in range(ch):
                humidity.append(int(round(float(parts[idx]))))
                idx += 1
            cm = int(parts[idx])
            idx += 1
            samples = []
            for _ in range(cm):
                samples.append(int(round(float(parts[idx]))))
                idx += 1
            if idx != len(parts):
                continue
        except (ValueError, TypeError, IndexError):
            continue
        if not humidity and not samples:
            # A percent-less line (e.g. temperature alone) can never come from
            # the server — air is only emitted alongside moisture — and
            # reaching draw() would blank the chart, so reject it.
            continue
        return {
            "node": node,
            "dry": dry,
            "wet": wet,
            "temp": temp,
            "humidity": humidity,
            "samples": samples,
        }
    return None


def y_window(dry, wet, *series):
    """Percent range [lo, hi] to draw, always including the band.

    A minimum span keeps a nearly-flat series readable instead of amplifying
    one-point jitter across the whole screen height.
    """
    values = []
    for src in series:
        values.extend(src)
    if dry is not None and dry >= 0:
        values.append(dry)
    if wet is not None and wet >= 0:
        values.append(wet)
    if not values:
        # Defense in depth: draw() only calls us with at least one percent
        # series, but an empty window must not crash min()/max() below.
        return 0, 100
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


def temp_window(temps):
    """Auto-scaled °C range [lo, hi] for the temperature trace."""
    lo = float(min(temps))
    hi = float(max(temps))
    pad = max(1.5, (hi - lo) * 0.2)
    lo -= pad
    hi += pad
    if hi - lo < 4:
        mid = (hi + lo) / 2.0
        lo, hi = mid - 2, mid + 2
    return lo, hi


def y_px(value, lo, hi, top=PLOT_T, bottom=PLOT_B):
    """Map a value to a screen row (y grows downward)."""
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


def _trace(tft, values, lo, hi, color, label=False, thick=False):
    """Draw one time series across the plot box.

    Returns the number of line segments drawn.  The newest sample gets a 2x2
    marker (so it reads as a dot, not a stray pixel); *label* additionally
    prints its value next to it for the primary soil series.  *thick* draws
    each segment as a 2-px bar so a flat series is still visibly a trace (the
    air-temperature line can otherwise vanish into the axis when flat).
    """
    count = len(values)
    if count < 2:
        if count == 1:
            tft.pixel(x_px(0, count), y_px(values[0], lo, hi), color)
        return 0
    px = x_px(0, count)
    py = y_px(values[0], lo, hi)
    tft.pixel(px, py, color)
    points = 0
    for i in range(1, count):
        x = x_px(i, count)
        y = y_px(values[i], lo, hi)
        _line(tft, px, py, x, y, color)
        if thick:
            _line(tft, px, py + 1, x, y + 1, color)
        px, py = x, y
        points += 1
    tft.pixel(px, py, color)
    tft.pixel(px - 1, py, color)
    tft.pixel(px, py - 1, color)
    tft.pixel(px - 1, py - 1, color)
    if thick:
        for oy in (1, 2):
            for ox in (0, -1):
                tft.pixel(px + ox, py + oy, color)
    if label:
        tft.text(str(values[-1]), max(PLOT_L, px - 16), max(PLOT_T, py - 16), WHITE)
    return points


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
        humidity = list(data.get("humidity") or [])
        temps = list(data.get("temp") or [])
        dry = data.get("dry")
        wet = data.get("wet")

        tft.fill(0x000000)
        soil_text = f"{samples[-1]}%" if samples else "--"
        hum_text = f"{humidity[-1]}%" if humidity else "--"
        air_text = f"{temps[-1]:.0f}C" if temps else "--"
        tft.text(f"SOIL {soil_text}  HUM {hum_text}  AIR {air_text}", 4, 2, WHITE)
        dry_label = "--" if dry is None or dry < 0 else dry
        wet_label = "--" if wet is None or wet < 0 else wet
        tft.text(f"dry {dry_label}  wet {wet_label}  n={len(samples)}", 4, H - 11, GREY)

        if len(samples) < 2 and len(humidity) < 2 and len(temps) < 2:
            tft.text("waiting for samples", 40, 62, GREY)
            return result

        # Percent axis (soil + humidity); temperature maps onto its own scale.
        lo, hi = y_window(dry, wet, samples, humidity)
        tlo, thi = temp_window(temps) if temps else (0, 0)

        # Frame + percentage labels on the left.
        _line(tft, PLOT_L, PLOT_T, PLOT_R, PLOT_T, GREY)
        _line(tft, PLOT_L, PLOT_B, PLOT_R, PLOT_B, GREY)
        _line(tft, PLOT_L, PLOT_T, PLOT_L, PLOT_B, GREY)
        _line(tft, PLOT_R, PLOT_T, PLOT_R, PLOT_B, GREY)
        tft.text(str(int(round(hi))), 2, PLOT_T, GREY)
        tft.text(str(int(round(lo))), 2, PLOT_B - 8, GREY)

        # Soil thresholds (the node's active band), each labelled with its
        # value in its own colour so the boundaries read without
        # cross-referencing the axis.  Only drawn with the soil series, since
        # they are soil thresholds.
        if samples:
            if dry is not None and dry >= 0:
                y_dry = y_px(dry, lo, hi)
                _line(tft, PLOT_L + 1, y_dry, PLOT_R - 1, y_dry, DRY_COLOR)
                tft.text(str(dry), PLOT_L + 3, max(PLOT_T, y_dry - 7), DRY_COLOR)
            if wet is not None and wet >= 0:
                y_wet = y_px(wet, lo, hi)
                _line(tft, PLOT_L + 1, y_wet, PLOT_R - 1, y_wet, WET_COLOR)
                tft.text(str(wet), PLOT_L + 3, max(PLOT_T, y_wet - 7), WET_COLOR)

        # Soil-moisture trace (white) with the newest value labelled.
        result["points"] += _trace(tft, samples, lo, hi, SOIL_COLOR, label=True)
        # Air humidity (green) shares the percent axis.
        result["points"] += _trace(tft, humidity, lo, hi, HUM_COLOR)
        # Air temperature (orange) on its own auto-scaled °C axis, drawn thick
        # (a flat temperature can otherwise vanish into the axis) and labelled
        # with its value at the newest point so it never reads as a frame line.
        if temps:
            result["points"] += _trace(tft, temps, tlo, thi, TEMP_COLOR, thick=True)
            tft.text(str(int(round(thi))), _TEMP_AXIS_X, PLOT_T, TEMP_COLOR)
            tft.text(str(int(round(tlo))), _TEMP_AXIS_X, PLOT_B - 8, TEMP_COLOR)
            lx = x_px(len(temps) - 1, len(temps))
            ly = y_px(temps[-1], tlo, thi)
            tft.text(f"{temps[-1]:.0f}C", max(PLOT_L, lx - 24), max(PLOT_T, ly - 7), TEMP_COLOR)

        return result
    except Exception as exc:  # noqa: BLE001 — display drivers vary; never kill the loop
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
