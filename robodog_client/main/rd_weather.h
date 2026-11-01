/*
 * rd_weather.h -- weather formatting for the RoboDog family-calendar panel.
 *
 * The server rides a tiny ``Weather`` submessage on every calendar reply
 * (``CalendarBundle.current`` = conditions now, ``CalEvent.weather`` =
 * forecast at the event's start).  The wire keeps tenths of a degree Celsius
 * and a raw WMO 4677 code because that is what the forecast source speaks;
 * the PANEL is read by a US household, so this module owns the two
 * display-side decisions and nothing else:
 *
 *   - WMO code  -> a short lowercase label that fits the row width
 *   - temp_c10  -> whole degrees Fahrenheit (rounded, half away from zero)
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

/* Short label for a WMO 4677 weather code as returned by Open-Meteo.
 *
 * Labels are capped at 13 chars: the widest footer is "at HH:MM " (9) +
 * "<t>°F " (6, incl. a minus sign) + label = 28 chars, comfortably inside the
 * panel's 388 px text band at the 10x20 font's 10 px/char (38 chars).  Codes are
 * grouped the way a person reads them: 51/53/55 are one "drizzle", 61/63/65
 * one "rain", 71/73/75 one "snow" -- the intensity is carried by the
 * temperature and the row context, not by a longer word.  Unknown codes
 * return "wx?" (never NULL) so a future WMO code degrades visibly. */
const char *rd_wmo_label(uint32_t code);

/* Tenths of a degree Celsius -> whole degrees Fahrenheit.
 *   f = round(c10 * 9 / 50) + 32, rounding half AWAY FROM ZERO so
 *   -2.5 °C -> 27 °F (not 28 via a truncating cast) and 0.5 °C -> 33 °F. */
int rd_temp_c10_to_f(int32_t c10);

/* Format one reading as "<deg>F <label>", e.g. "46\xB0" "F clear".
 *
 * The degree sign is emitted as the raw byte 0xB0: u8g2_font_10x20_tf carries
 * that latin-1 glyph and the firmware draws bytes verbatim (no UTF-8 decode),
 * so a multi-byte "\xC2\xB0" would render as two garbage glyphs.
 *
 * Returns 1 when written, 0 when the slot carries no forecast (present ==
 * false) or the buffer is too small -- callers draw NOTHING in that case, so
 * a missing forecast can never masquerade as "0F clear". */
int rd_format_weather(const lma_weather_t *wx, char *out, size_t cap);

#ifdef __cplusplus
}
#endif

#endif /* RD_WEATHER_H */
