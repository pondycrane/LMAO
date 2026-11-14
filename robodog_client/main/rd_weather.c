/*
 * rd_weather.c -- WMO code -> icon kind, Celsius -> Fahrenheit and the panel's
 * bare temperature string (see rd_weather.h).
 *
 * Deliberately dependency-free (stdlib + lma_proto.h only): the firmware
 * compiles this file, and rd_weather_selftest.c proves the same source on a
 * bare host `cc`.
 */
#include "rd_weather.h"

#include <stdio.h>

/* Degree sign in the 10x20 font's latin-1 slot (see rd_weather.h). */
#define RD_DEG "\xB0"

rd_wx_icon_t rd_weather_icon_kind(uint32_t code)
{
    switch (code) {
    case 0:  /* fallthrough */
    case 1:  return RD_WX_SUN;
    case 2:  return RD_WX_SUN_CLOUD;
    case 3:  return RD_WX_CLOUD;
    case 45: /* fallthrough */
    case 48: return RD_WX_FOG;
    case 51: /* fallthrough */
    case 53: /* fallthrough */
    case 55: /* fallthrough */
    case 56: /* fallthrough */
    case 57: return RD_WX_RAIN;
    case 61: /* fallthrough */
    case 63: /* fallthrough */
    case 65: /* fallthrough */
    case 66: /* fallthrough */
    case 67: return RD_WX_RAIN;
    case 71: /* fallthrough */
    case 73: /* fallthrough */
    case 75: /* fallthrough */
    case 77: return RD_WX_SNOW;
    case 80: /* fallthrough */
    case 81: /* fallthrough */
    case 82: return RD_WX_RAIN;
    case 85: /* fallthrough */
    case 86: return RD_WX_SNOW;
    case 95: /* fallthrough */
    case 96: /* fallthrough */
    case 99: return RD_WX_THUNDER;
    default: return RD_WX_UNKNOWN;
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

int rd_format_temp(const lma_weather_t *wx, char *out, size_t cap)
{
    if (wx == NULL || out == NULL || cap == 0 || !wx->present) {
        if (out != NULL && cap > 0) {
            out[0] = '\0';
        }
        return 0;
    }
    const int f = rd_temp_c10_to_f(wx->temp_c10);
    const int n = snprintf(out, cap, "%d" RD_DEG "F", f);
    if (n < 0 || (size_t)n >= cap) {
        out[0] = '\0'; /* never leave a truncated fragment on screen */
        return 0;
    }
    return 1;
}
