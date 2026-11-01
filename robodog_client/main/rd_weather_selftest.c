/*
 * rd_weather_selftest.c -- host-only self-test for rd_weather.c (the panel's
 * weather formatting).  NOT part of the ESP-IDF build (defines main()).
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

/* Every label the panel can print must fit the row budget: the widest footer
 * is "at HH:MM " (9) + "<t>°F " (6 with a minus sign) + label = 28 chars, and
 * the panel's text band is 388 px at the 10x20 font's 10 px/char -- 38 chars.
 * Labels must stay <= 13 so that budget holds.  Asserted here so a new WMO
 * branch cannot silently widen the layout. */
static int test_labels(void)
{
    int passed = 0;
    const unsigned codes[] = {
        0, 1, 2, 3, 45, 48, 51, 53, 55, 56, 57, 61, 63, 65, 66, 67,
        71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99,
    };
    for (size_t i = 0; i < sizeof(codes) / sizeof(codes[0]); i++) {
        const char *l = rd_wmo_label(codes[i]);
        CHECK(l != NULL);
        CHECK(l[0] != '\0');
        CHECK(strlen(l) <= 13);
    }

    /* Representative branches (intensity collapses to one word each). */
    CHECK(strcmp(rd_wmo_label(0), "clear") == 0);
    CHECK(strcmp(rd_wmo_label(2), "partly cloudy") == 0);
    CHECK(strcmp(rd_wmo_label(53), "drizzle") == 0);
    CHECK(strcmp(rd_wmo_label(65), "rain") == 0);
    CHECK(strcmp(rd_wmo_label(73), "snow") == 0);
    CHECK(strcmp(rd_wmo_label(82), "showers") == 0);
    CHECK(strcmp(rd_wmo_label(99), "thunder hail") == 0);

    /* Unknown / out-of-range codes degrade visibly, never to NULL. */
    CHECK(strcmp(rd_wmo_label(4), "wx?") == 0);
    CHECK(strcmp(rd_wmo_label(1000), "wx?") == 0);

    printf("test_labels: %d checks passed\n", passed);
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
    CHECK(rd_format_weather(&wx, out, sizeof out) == 0);
    CHECK(out[0] == '\0');

    wx.present = true;
    wx.temp_c10 = 67;
    wx.code = 0;
    CHECK(rd_format_weather(&wx, out, sizeof out) == 1);
    CHECK(strcmp(out, "44\xB0" "F clear") == 0);

    wx.temp_c10 = -25;
    wx.code = 71;
    CHECK(rd_format_weather(&wx, out, sizeof out) == 1);
    CHECK(strcmp(out, "27\xB0" "F snow") == 0);

    /* The widest label at the coldest reading still fits a row buffer. */
    wx.temp_c10 = -500;
    wx.code = 85;
    CHECK(rd_format_weather(&wx, out, sizeof out) == 1);
    CHECK(strcmp(out, "-58\xB0" "F snow showers") == 0);
    CHECK(strlen(out) <= 20);

    /* A too-small buffer yields NO output (never a truncated fragment). */
    wx.temp_c10 = 67;
    wx.code = 0;
    CHECK(rd_format_weather(&wx, out, 5) == 0);
    CHECK(out[0] == '\0');

    /* NULL-safety contract. */
    CHECK(rd_format_weather(NULL, out, sizeof out) == 0);
    CHECK(rd_format_weather(&wx, NULL, sizeof out) == 0);
    CHECK(rd_format_weather(&wx, out, 0) == 0);

    printf("test_format: %d checks passed\n", passed);
    return 0;
}

int main(void)
{
    printf("rd_weather self-test\n");
    if (test_labels() != 0 ||
        test_celsius_to_fahrenheit() != 0 ||
        test_format() != 0) {
        printf("RESULT: FAIL\n");
        return 1;
    }
    printf("RESULT: PASS\n");
    return 0;
}
