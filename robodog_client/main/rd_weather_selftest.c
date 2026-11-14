/*
 * rd_weather_selftest.c -- host-only self-test for rd_weather.c (the panel's
 * weather presentation: WMO -> icon kind, Celsius -> Fahrenheit, the bare
 * temperature string).  NOT part of the ESP-IDF build (defines main()).
 *
 * Build & run (from robodog_client/):
 *   cc -std=c11 -Wall -Wextra main/rd_weather_selftest.c main/rd_weather.c \
 *      -o /tmp/rd_wx_selftest && /tmp/rd_wx_selftest
 */
#include <stdio.h>
#include <string.h>

#include "rd_weather.h"

#define CHECK(cond)                                                        \
    do {                                                                   \
        if (!(cond)) {                                                     \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
            return 1;                                                      \
        }                                                                  \
        passed++;                                                          \
    } while (0)

/* Every WMO code the server can forward must land on a DRAWN icon (only a
 * genuinely unmapped code may fall through to RD_WX_UNKNOWN), and each group
 * must land on the shape a person expects: intensity collapses to one shape
 * per group, so a new branch cannot silently turn rain into snow. */
static int test_icon_kinds(void)
{
    int passed = 0;

    CHECK(rd_weather_icon_kind(0) == RD_WX_SUN);
    CHECK(rd_weather_icon_kind(1) == RD_WX_SUN);
    CHECK(rd_weather_icon_kind(2) == RD_WX_SUN_CLOUD);
    CHECK(rd_weather_icon_kind(3) == RD_WX_CLOUD);
    CHECK(rd_weather_icon_kind(45) == RD_WX_FOG);
    CHECK(rd_weather_icon_kind(48) == RD_WX_FOG);

    /* Drizzle (51/53/55), freezing drizzle (56/57), rain (61/63/65),
     * freezing rain (66/67) and showers (80/81/82) are all RAIN shapes. */
    const unsigned rain[] = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82};
    for (size_t i = 0; i < sizeof(rain) / sizeof(rain[0]); i++) {
        CHECK(rd_weather_icon_kind(rain[i]) == RD_WX_RAIN);
    }

    /* Snow (71/73/75/77) and snow showers (85/86). */
    const unsigned snow[] = {71, 73, 75, 77, 85, 86};
    for (size_t i = 0; i < sizeof(snow) / sizeof(snow[0]); i++) {
        CHECK(rd_weather_icon_kind(snow[i]) == RD_WX_SNOW);
    }

    CHECK(rd_weather_icon_kind(95) == RD_WX_THUNDER);
    CHECK(rd_weather_icon_kind(96) == RD_WX_THUNDER);
    CHECK(rd_weather_icon_kind(99) == RD_WX_THUNDER);

    /* Unmapped / out-of-range codes degrade to NO icon (the temperature still
     * prints), never to a wrong shape and never to a crash. */
    CHECK(rd_weather_icon_kind(4) == RD_WX_UNKNOWN);
    CHECK(rd_weather_icon_kind(1000) == RD_WX_UNKNOWN);
    CHECK(rd_weather_icon_kind(0xFFFFFFFFu) == RD_WX_UNKNOWN);

    printf("test_icon_kinds: %d checks passed\n", passed);
    return 0;
}

static int test_celsius_to_fahrenheit(void)
{
    int passed = 0;
    /* 0 °C = 32 °F; 100 °C = 212 °F. */
    CHECK(rd_temp_c10_to_f(0) == 32);
    CHECK(rd_temp_c10_to_f(1000) == 212);
    /* Common household readings: 6.7 °C -> 44 °F, 1.9 °C -> 35 °F. */
    CHECK(rd_temp_c10_to_f(67) == 44);
    CHECK(rd_temp_c10_to_f(19) == 35);
    /* -2.5 °C = 27.5 °F -> 27 (half away from zero, NOT the truncating 28). */
    CHECK(rd_temp_c10_to_f(-25) == 27);
    /* 0.5 °C = 32.9 °F -> 33; -0.5 °C = 31.1 °F -> 31. */
    CHECK(rd_temp_c10_to_f(5) == 33);
    CHECK(rd_temp_c10_to_f(-5) == 31);
    /* -20 °C = -4 °F exactly; -40 °C = -40 °F (the crossover). */
    CHECK(rd_temp_c10_to_f(-200) == -4);
    CHECK(rd_temp_c10_to_f(-400) == -40);
    /* Round-trip sanity on the extremes the wire can carry (int32 tenths). */
    CHECK(rd_temp_c10_to_f(500) == 122);
    CHECK(rd_temp_c10_to_f(-500) == -58);

    printf("test_celsius_to_fahrenheit: %d checks passed\n", passed);
    return 0;
}

static int test_format(void)
{
    int passed = 0;
    char out[32];
    lma_weather_t wx = {0};

    /* Absent forecast: nothing written, nothing drawn. */
    CHECK(rd_format_temp(&wx, out, sizeof out) == 0);
    CHECK(out[0] == '\0');

    wx.present = true;
    wx.temp_c10 = 67;
    wx.code = 0;
    CHECK(rd_format_temp(&wx, out, sizeof out) == 1);
    CHECK(strcmp(out, "44\xB0" "F") == 0);

    /* Negative readings keep their sign; the widest reading the wire can
     * carry is 5 glyphs at 10 px = 50 px. */
    wx.temp_c10 = -25;
    CHECK(rd_format_temp(&wx, out, sizeof out) == 1);
    CHECK(strcmp(out, "27\xB0" "F") == 0);

    wx.temp_c10 = -500;
    CHECK(rd_format_temp(&wx, out, sizeof out) == 1);
    CHECK(strcmp(out, "-58\xB0" "F") == 0);
    CHECK(strlen(out) == 5);

    /* A too-small buffer yields NO output (never a truncated fragment). */
    wx.temp_c10 = 67;
    CHECK(rd_format_temp(&wx, out, 4) == 0);
    CHECK(out[0] == '\0');

    /* NULL-safety contract. */
    CHECK(rd_format_temp(NULL, out, sizeof out) == 0);
    CHECK(rd_format_temp(&wx, NULL, sizeof out) == 0);
    CHECK(rd_format_temp(&wx, out, 0) == 0);

    printf("test_format: %d checks passed\n", passed);
    return 0;
}

int main(void)
{
    printf("rd_weather self-test\n");
    if (test_icon_kinds() != 0 ||
        test_celsius_to_fahrenheit() != 0 ||
        test_format() != 0) {
        printf("RESULT: FAIL\n");
        return 1;
    }
    printf("RESULT: PASS\n");
    return 0;
}
