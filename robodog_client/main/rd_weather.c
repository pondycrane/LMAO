/*
 * rd_weather.c -- WMO code labelling + Celsius-to-Fahrenheit conversion for
 * the RoboDog panel (see rd_weather.h).
 *
 * Deliberately dependency-free (stdlib + lma_proto.h only): the firmware
 * compiles this file, and rd_weather_selftest.c proves the same source on a
 * bare host `cc`.
 */
#include "rd_weather.h"

#include <stdio.h>

/* Degree sign in the 10x20 font's latin-1 slot (see rd_weather.h). */
#define RD_DEG "\xB0"

const char *rd_wmo_label(uint32_t code)
{
    switch (code) {
    case 0:  return "clear";
    case 1:  return "mostly clear";
    case 2:  return "partly cloudy";
    case 3:  return "overcast";
    case 45: return "fog";
    case 48: return "rime fog";
    case 51: /* fallthrough */
    case 53: /* fallthrough */
    case 55: return "drizzle";
    case 56: /* fallthrough */
    case 57: return "frz drizzle";
    case 61: /* fallthrough */
    case 63: /* fallthrough */
    case 65: return "rain";
    case 66: /* fallthrough */
    case 67: return "frz rain";
    case 71: /* fallthrough */
    case 73: /* fallthrough */
    case 75: return "snow";
    case 77: return "snow grains";
    case 80: /* fallthrough */
    case 81: /* fallthrough */
    case 82: return "showers";
    case 85: /* fallthrough */
    case 86: return "snow showers";
    case 95: return "thunder";
    case 96: /* fallthrough */
    case 99: return "thunder hail";
    default: return "wx?";
    }
}

int rd_temp_c10_to_f(int32_t c10)
{
    /* tenths of a degree Fahrenheit offset: c10 * 9 / 5 */
    int32_t f10 = (int32_t)(((int64_t)c10 * 9) / 5);
    /* Round half away from zero, then add the 32 F offset. */
    int32_t f = (f10 >= 0) ? (f10 + 5) / 10 : (f10 - 5) / 10;
    return (int)(f + 32);
}

int rd_format_weather(const lma_weather_t *wx, char *out, size_t cap)
{
    if (wx == NULL || out == NULL || cap == 0 || !wx->present) {
        if (out != NULL && cap > 0) {
            out[0] = '\0';
        }
        return 0;
    }
    const int f = rd_temp_c10_to_f(wx->temp_c10);
    const int n = snprintf(out, cap, "%d" RD_DEG "F %s", f,
                           rd_wmo_label(wx->code));
    if (n < 0 || (size_t)n >= cap) {
        out[0] = '\0'; /* never leave a truncated fragment on screen */
        return 0;
    }
    return 1;
}
