/*
 * rd_weather.h -- weather presentation for the RoboDog family-calendar panel.
 *
 * The server rides a tiny ``Weather`` submessage on every calendar reply
 * (``CalendarBundle.current`` = conditions now, ``CalEvent.weather`` =
 * forecast at the event's start).  The wire keeps tenths of a degree Celsius
 * and a raw WMO 4677 code because that is what the forecast source speaks;
 * the PANEL is read by a US household, so this module owns the two display-side
 * decisions and nothing else:
 *
 *   - WMO code  -> an ICON KIND (the renderer draws it as a 16x16 shape)
 *   - temp_c10  -> whole degrees Fahrenheit (rounded, half away from zero)
 *
 * The condition used to be a lowercase word ("partly cloudy"), which is the
 * widest thing on the panel: it consumed the whole 204 px headline column and
 * clipped its own content ("forecast 43°F ove...").  A shape says the same
 * thing in 16 px and does not compete with the readings for width.
 *
 * Deliberately dependency-free (stdlib + lma_proto.h only), so it compiles and
 * self-tests on a bare host `cc -std=c11 -Wall -Wextra` as well as inside the
 * ESP-IDF main component (rd_weather_selftest.c is the host gate).
 */
#ifndef RD_WEATHER_H
#define RD_WEATHER_H

#include <stddef.h>
#include <stdint.h>

#include "lma_proto.h"

#ifdef __cplusplus
extern "C" {
#endif

/* The icon vocabulary.  Seven shapes, grouped the way a person reads the
 * forecast: intensity (drizzle vs rain, light vs heavy snow) is carried by the
 * temperature and the event context, not by a second glyph -- the panel has
 * one raster per role and no type scale, so a distinction that cannot be seen
 * at 16 px is not a distinction worth drawing. */
typedef enum {
    RD_WX_UNKNOWN = 0,   /* unmapped code: draw the temperature alone */
    RD_WX_SUN,           /* 0 clear, 1 mainly clear */
    RD_WX_SUN_CLOUD,     /* 2 partly cloudy */
    RD_WX_CLOUD,         /* 3 overcast */
    RD_WX_FOG,           /* 45 fog, 48 depositing rime fog */
    RD_WX_RAIN,          /* 51-57 drizzle, 61-67 rain, 80-82 showers */
    RD_WX_SNOW,          /* 71-77 snow, 85-86 snow showers */
    RD_WX_THUNDER,       /* 95, 96, 99 thunderstorm (with/without hail) */
} rd_wx_icon_t;

/* WMO 4677 code (as returned by Open-Meteo) -> icon kind.  Never fails: an
 * unmapped code returns RD_WX_UNKNOWN, which the renderer draws as NO icon
 * (the temperature still prints, so a future code degrades to a reading
 * instead of a wrong picture). */
rd_wx_icon_t rd_weather_icon_kind(uint32_t code);

/* Tenths of a degree Celsius -> whole degrees Fahrenheit.
 *   f = round(c10 * 9 / 50) + 32, rounding half AWAY FROM ZERO so
 *   -2.5 °C -> 27 °F (not 28 via a truncating cast) and 0.5 °C -> 33 °F. */
int rd_temp_c10_to_f(int32_t c10);

/* Format one reading as "<deg>F", e.g. "37\xB0" "F" / "-4\xB0" "F".
 *
 * The degree sign is emitted as the raw byte 0xB0: u8g2_font_10x20_tf carries
 * that latin-1 glyph and the firmware draws bytes verbatim (no UTF-8 decode),
 * so a multi-byte "\xC2\xB0" would render as two garbage glyphs.
 *
 * Returns 1 when written, 0 when the slot carries no forecast (present ==
 * false) or the buffer is too small -- callers draw NOTHING in that case, so
 * a missing forecast can never masquerade as "0F". */
int rd_format_temp(const lma_weather_t *wx, char *out, size_t cap);

#ifdef __cplusplus
}
#endif

#endif /* RD_WEATHER_H */
