/*
 * rd_chart_selftest.c -- host test for rd_chart.c (chart geometry).
 *
 * Host-only (defines main); not part of the IDF build.
 *   cc -std=c11 -Wall -Wextra main/rd_chart_selftest.c main/rd_chart.c \
 *      -o /tmp/rd_chart_test && /tmp/rd_chart_test
 */
#include <stdio.h>
#include <stdlib.h>

#include "rd_chart.h"

static int failures = 0;

#define CHECK(cond)                                                          \
    do {                                                                     \
        if (!(cond)) {                                                       \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);  \
            failures++;                                                      \
        }                                                                    \
    } while (0)

static void test_empty_series(void)
{
    rd_chart_plan_t plan;
    rd_chart_plan(NULL, 0, NULL, 0, 10, 20, 390, 200, &plan);
    CHECK(plan.temp_n == 0);
    CHECK(plan.hum_n == 0);
    CHECK(plan.temp_min == 0 && plan.temp_max == 0);
    CHECK(plan.hum_min == 0 && plan.hum_max == 0);
    CHECK(rd_chart_y(&plan, false, 215) == plan.y1);
    CHECK(rd_chart_y(&plan, true, 55) == plan.y1);
}

static void test_extents_and_spread(void)
{
    const uint32_t temp[] = {214, 220, 226};
    const uint32_t hum[] = {50, 55, 60};
    rd_chart_plan_t plan;
    rd_chart_plan(temp, 3, hum, 3, 10, 20, 390, 200, &plan);

    CHECK(plan.temp_n == 3 && plan.hum_n == 3);
    CHECK(plan.temp_min == 214 && plan.temp_max == 226);
    CHECK(plan.hum_min == 50 && plan.hum_max == 60);

    /* Oldest at the left axis, newest at the right axis. */
    CHECK(plan.temp[0].x == 10 && plan.temp[2].x == 390);
    CHECK(plan.hum[0].x == 10 && plan.hum[2].x == 390);

    /* Highest value at the top, lowest at the bottom. */
    CHECK(plan.temp[0].y == 200 && plan.temp[2].y == 20);
    CHECK(plan.hum[0].y == 200 && plan.hum[2].y == 20);

    /* Monotonic y within a series (higher value -> smaller y). */
    CHECK(plan.temp[1].y < plan.temp[0].y && plan.temp[1].y > plan.temp[2].y);
}

static void test_single_sample_sits_at_now(void)
{
    const uint32_t temp[] = {215};
    rd_chart_plan_t plan;
    rd_chart_plan(temp, 1, NULL, 0, 10, 20, 390, 200, &plan);
    CHECK(plan.temp_n == 1);
    CHECK(plan.temp[0].x == 390);
    /* A single sample has no trend: the extent is widened to the minimum span
     * around it, so the sample lands mid-box rather than on an axis. */
    CHECK(plan.temp_max - plan.temp_min == RD_CHART_MIN_SPAN_TEMP_C10);
    CHECK(plan.temp[0].y > 20 && plan.temp[0].y < 200);
    CHECK(rd_chart_y(&plan, false, 215) == plan.temp[0].y);
}

static void test_flat_series_is_widened_not_stretched(void)
{
    const uint32_t hum[] = {55, 55, 55};
    rd_chart_plan_t plan;
    rd_chart_plan(NULL, 0, hum, 3, 10, 20, 390, 200, &plan);
    CHECK(plan.hum_max - plan.hum_min == RD_CHART_MIN_SPAN_HUM_PCT);
    /* All three samples plot at the same y (flat line), mid-box. */
    CHECK(plan.hum[0].y == plan.hum[1].y && plan.hum[1].y == plan.hum[2].y);
    CHECK(plan.hum[0].y > 20 && plan.hum[0].y < 200);
    /* A value outside the widened band still clamps. */
    CHECK(rd_chart_y(&plan, true, 0) == 200);
    CHECK(rd_chart_y(&plan, true, 100) == 20);
}

static void test_narrow_but_real_variation_stays_small(void)
{
    /* 30.5 -> 30.6 degC is a 1-tenth span; widened to 1 degC it must NOT
     * swing across the whole plot (the live board's original defect). */
    const uint32_t temp[] = {305, 306, 305, 306};
    rd_chart_plan_t plan;
    rd_chart_plan(temp, 4, NULL, 0, 10, 20, 390, 200, &plan);
    CHECK(plan.temp_max - plan.temp_min == RD_CHART_MIN_SPAN_TEMP_C10);
    int ymin = plan.temp[0].y, ymax = plan.temp[0].y;
    for (size_t i = 1; i < plan.temp_n; i++) {
        if (plan.temp[i].y < ymin) ymin = plan.temp[i].y;
        if (plan.temp[i].y > ymax) ymax = plan.temp[i].y;
    }
    const int swing = ymax - ymin;
    CHECK(swing > 0);          /* the 0.1 degC step is visible */
    CHECK(swing <= 30);        /* but it is a tenth of the 180 px box */
}

static void test_humidity_extent_never_goes_negative(void)
{
    const uint32_t hum[] = {1, 2};
    rd_chart_plan_t plan;
    rd_chart_plan(NULL, 0, hum, 2, 10, 20, 390, 200, &plan);
    CHECK(plan.hum_min == 0);
    CHECK(plan.hum_max == RD_CHART_MIN_SPAN_HUM_PCT);
}

static void test_overflow_keeps_the_newest(void)
{
    uint32_t temp[RD_CHART_MAX_POINTS + 10];
    for (size_t i = 0; i < sizeof(temp) / sizeof(temp[0]); i++) temp[i] = (uint32_t)(200 + i);
    rd_chart_plan_t plan;
    rd_chart_plan(temp, sizeof(temp) / sizeof(temp[0]), NULL, 0, 0, 0, 100, 100, &plan);
    CHECK(plan.temp_n == RD_CHART_MAX_POINTS);
    CHECK(plan.temp[0].y <= 100 && plan.temp[plan.temp_n - 1].y >= 0);
    /* Newest sample is the maximum -> top of the box. */
    CHECK(plan.temp[plan.temp_n - 1].y == 0);
    /* Oldest kept sample is the minimum -> bottom of the box. */
    CHECK(plan.temp[0].y == 100);
}

static void test_y_clamps_out_of_range(void)
{
    const uint32_t temp[] = {200, 220};
    rd_chart_plan_t plan;
    rd_chart_plan(temp, 2, NULL, 0, 0, 10, 100, 110, &plan);
    CHECK(rd_chart_y(&plan, false, 0) == 110);    /* below min -> bottom */
    CHECK(rd_chart_y(&plan, false, 999) == 10);   /* above max -> top */
    CHECK(rd_chart_y(&plan, false, 210) == 60);   /* midpoint */
}

int main(void)
{
    test_empty_series();
    test_extents_and_spread();
    test_single_sample_sits_at_now();
    test_flat_series_is_widened_not_stretched();
    test_narrow_but_real_variation_stays_small();
    test_humidity_extent_never_goes_negative();
    test_overflow_keeps_the_newest();
    test_y_clamps_out_of_range();

    if (failures) {
        fprintf(stderr, "%d check(s) failed\n", failures);
        return 1;
    }
    printf("rd_chart_selftest: all checks passed\n");
    return 0;
}
