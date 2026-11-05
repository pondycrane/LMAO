/*
 * rd_chart.c -- chart geometry for the air temperature/humidity history
 * (see rd_chart.h).  Pure C11: no u8g2, no ESP-IDF, so the host self-test
 * (rd_chart_selftest.c) compiles exactly this file.
 */
#include "rd_chart.h"

#include <stdio.h>

static void extent(const uint32_t* values, size_t n, uint32_t* lo, uint32_t* hi)
{
    *lo = 0;
    *hi = 0;
    for (size_t i = 0; i < n; i++) {
        if (i == 0 || values[i] < *lo) *lo = values[i];
        if (i == 0 || values[i] > *hi) *hi = values[i];
    }
}

/* Widen a too-narrow extent symmetrically around its midpoint so small
 * sample-to-sample noise does not fill the plot.  The low end never goes
 * below 0 (humidity has no negative values). */
static void widen(uint32_t* lo, uint32_t* hi, uint32_t min_span)
{
    if (*hi - *lo >= min_span) return;
    const uint32_t mid = (*lo + *hi) / 2u;
    const uint32_t half = min_span / 2u;
    *lo = (mid > half) ? mid - half : 0u;
    *hi = *lo + min_span;
}

/* Copy the newest (up to) RD_CHART_MAX_POINTS samples, oldest first. */
static size_t tail(const uint32_t* values, size_t n, uint32_t* out)
{
    const size_t keep = n > RD_CHART_MAX_POINTS ? RD_CHART_MAX_POINTS : n;
    const size_t first = n - keep;
    for (size_t i = 0; i < keep; i++) out[i] = values[first + i];
    return keep;
}

static void plot(const uint32_t* values, size_t n, int x0, int y0, int x1, int y1,
                 uint32_t lo, uint32_t hi, rd_point_t* out)
{
    if (n == 0) return;
    const int span = x1 - x0;
    for (size_t i = 0; i < n; i++) {
        /* One sample per x position; a single sample sits at the right edge
         * (the "now" end of the chart). */
        const int x = (n == 1) ? x1 : x0 + (int)((long)span * (long)i / (long)(n - 1));
        int y;
        if (hi == lo) {
            y = y1;
        } else {
            const long v = (long)values[i] - (long)lo;
            const long range = (long)hi - (long)lo;
            y = y1 - (int)(((long)(y1 - y0) * v) / range);
        }
        if (y < y0) y = y0;
        if (y > y1) y = y1;
        out[i].x = x;
        out[i].y = y;
    }
}

void rd_chart_plan(const uint32_t* temp, size_t n_temp,
                   const uint32_t* hum, size_t n_hum,
                   int x0, int y0, int x1, int y1,
                   rd_chart_plan_t* plan)
{
    if (plan == NULL) return;
    plan->x0 = x0;
    plan->y0 = y0;
    plan->x1 = x1;
    plan->y1 = y1;
    plan->start_ms = 0;
    plan->period_ms = 0;

    uint32_t t[RD_CHART_MAX_POINTS];
    uint32_t h[RD_CHART_MAX_POINTS];
    plan->temp_n = temp ? tail(temp, n_temp, t) : 0;
    plan->hum_n = hum ? tail(hum, n_hum, h) : 0;

    extent(t, plan->temp_n, &plan->temp_min, &plan->temp_max);
    extent(h, plan->hum_n, &plan->hum_min, &plan->hum_max);
    if (plan->temp_n > 0) {
        widen(&plan->temp_min, &plan->temp_max, RD_CHART_MIN_SPAN_TEMP_C10);
    }
    if (plan->hum_n > 0) {
        widen(&plan->hum_min, &plan->hum_max, RD_CHART_MIN_SPAN_HUM_PCT);
    }

    plot(t, plan->temp_n, x0, y0, x1, y1, plan->temp_min, plan->temp_max, plan->temp);
    plot(h, plan->hum_n, x0, y0, x1, y1, plan->hum_min, plan->hum_max, plan->hum);
}

int rd_chart_y(const rd_chart_plan_t* plan, bool humidity, uint32_t value)
{
    if (plan == NULL) return 0;
    const uint32_t lo = humidity ? plan->hum_min : plan->temp_min;
    const uint32_t hi = humidity ? plan->hum_max : plan->temp_max;
    if (hi == lo) return plan->y1;
    const long range = (long)hi - (long)lo;
    const long v = (long)value - (long)lo;
    int y = plan->y1 - (int)(((long)(plan->y1 - plan->y0) * v) / range);
    if (y < plan->y0) y = plan->y0;
    if (y > plan->y1) y = plan->y1;
    return y;
}

/* ---- Time axis ---------------------------------------------------------- */

#define RD_CHART_NOMINAL_PERIOD_MS 300000u /* 5 min, matches the server's env ring */

void rd_chart_set_time(rd_chart_plan_t* plan, uint64_t start_ms,
                       uint32_t period_ms, size_t total_samples)
{
    if (plan == NULL) return;
    plan->period_ms = period_ms ? period_ms : RD_CHART_NOMINAL_PERIOD_MS;
    /* The plan keeps the newest RD_CHART_MAX_POINTS samples, so a longer
     * series starts later than start_ms by however many were dropped. */
    const size_t kept = plan->temp_n > plan->hum_n ? plan->temp_n : plan->hum_n;
    const size_t dropped = total_samples > kept ? total_samples - kept : 0;
    plan->start_ms = start_ms + (uint64_t)dropped * (uint64_t)plan->period_ms;
}

int rd_chart_x(const rd_chart_plan_t* plan, size_t i, size_t n)
{
    if (plan == NULL) return 0;
    if (n == 0) return plan->x1;
    if (i >= n) i = n - 1;
    /* One sample per x position; a lone sample sits at the right edge (the
     * "now" end), exactly like plot() places the traces. */
    if (n == 1) return plan->x1;
    const int span = plan->x1 - plan->x0;
    return plan->x0 + (int)((long)span * (long)i / (long)(n - 1));
}

uint64_t rd_chart_sample_ms(const rd_chart_plan_t* plan, size_t i, size_t n)
{
    if (plan == NULL || plan->start_ms == 0) return 0;
    if (n == 0) return plan->start_ms;
    if (i >= n) i = n - 1;
    return plan->start_ms + (uint64_t)i * (uint64_t)plan->period_ms;
}

int rd_chart_format_hm(uint64_t epoch_ms, int tz_offset_min, char* out,
                       size_t cap)
{
    if (out == NULL || cap == 0) return 0;
    if (epoch_ms < 1000) { /* no clock: never print a fake 00:00 */
        snprintf(out, cap, "--:--");
        return 0;
    }
    /* Civil time by arithmetic (no gmtime/localtime: the device has no tz
     * database and the offset is a build-time constant). */
    int64_t secs = (int64_t)(epoch_ms / 1000) +
                   (int64_t)tz_offset_min * 60;
    int64_t days = secs / 86400;
    if (secs < 0 && secs % 86400) days -= 1; /* floor for pre-epoch offsets */
    int64_t sod = secs - days * 86400;
    const int hh = (int)(sod / 3600);
    const int mm = (int)((sod % 3600) / 60);
    snprintf(out, cap, "%02d:%02d", hh, mm);
    return 1;
}

void rd_chart_time_labels(const rd_chart_plan_t* plan, size_t n,
                          int tz_offset_min,
                          char out[RD_CHART_TICKS][RD_CHART_LABEL_LEN])
{
    /* Left edge, middle, right edge.  n==1 collapses all three onto "now". */
    const size_t idx[RD_CHART_TICKS] = {0, n / 2, n ? n - 1 : 0};
    for (int k = 0; k < RD_CHART_TICKS; k++) {
        rd_chart_format_hm(rd_chart_sample_ms(plan, idx[k], n), tz_offset_min,
                           out[k], RD_CHART_LABEL_LEN);
    }
}
