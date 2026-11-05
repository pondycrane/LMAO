#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Chart geometry for the RoboDog home client's air history.
 *
 * Values arrive from the server's ChartBundle: temp in TENTHS of a degree
 * Celsius, hum in integer percent (see firmware_common/lma_common/lma_chart.h).
 *
 * This file is PURE arithmetic (no u8g2, no ESP-IDF), so the geometry is
 * host-testable with `cc -std=c11`; the panel drawing lives in ui.cpp and only
 * plots these points.
 */

#define RD_CHART_MAX_POINTS 64

/* Minimum plotted span per series.  A nearly flat series (30.5 vs 30.6 degC)
 * would otherwise be stretched across the whole box, so every 0.1 degC sample
 * looks like a full-height swing.  The extent is widened symmetrically around
 * the midpoint until it covers at least this span. */
#define RD_CHART_MIN_SPAN_TEMP_C10 10u /* 1.0 degC */
#define RD_CHART_MIN_SPAN_HUM_PCT 5u   /* 5 %      */

typedef struct {
    int x, y;
} rd_point_t;

typedef struct {
    /* Series extents (0 when the series is empty). */
    uint32_t temp_min, temp_max;   /* tenths of degC */
    uint32_t hum_min, hum_max;     /* percent */
    /* Plot box, inclusive of the axes; traces live inside it. */
    int x0, y0, x1, y1;
    /* Traces (oldest -> newest). */
    size_t temp_n, hum_n;
    rd_point_t temp[RD_CHART_MAX_POINTS];
    rd_point_t hum[RD_CHART_MAX_POINTS];
    /* Time axis (from ChartBundle.start_ms / period_ms).  Sample i is at
     * start_ms + i*period_ms; start_ms 0 means the device had no clock. */
    uint64_t start_ms;
    uint32_t period_ms;
} rd_chart_plan_t;

/* Label buffer for one x-axis tick ("HH:MM" + NUL). */
#define RD_CHART_LABEL_LEN 6
/* Ticks drawn on the time axis: left edge, middle, right edge ("now"). */
#define RD_CHART_TICKS 3

/* Fill *plan for a series pair inside the box (x0,y0)-(x1,y1).
 * `temp`/`hum` are oldest-first; more than RD_CHART_MAX_POINTS samples keep the
 * NEWEST ones (the chart's right edge is "now").  Empty series leave their
 * point count at 0 and their extents at 0.  The time axis is unset (0); call
 * rd_chart_set_time() with the bundle's start_ms/period_ms afterwards. */
void rd_chart_plan(const uint32_t* temp, size_t n_temp,
                   const uint32_t* hum, size_t n_hum,
                   int x0, int y0, int x1, int y1,
                   rd_chart_plan_t* plan);

/* Set the time axis: the epoch-ms of the OLDEST sample in the full series, the
 * sample spacing, and how many samples that series had.
 *
 * `total_samples` matters because rd_chart_plan() keeps only the newest
 * RD_CHART_MAX_POINTS points: when the series was longer, the visible window
 * starts later, so the axis origin slides forward by the dropped count times
 * the period.  period_ms 0 falls back to a nominal 5 min. */
void rd_chart_set_time(rd_chart_plan_t* plan, uint64_t start_ms,
                       uint32_t period_ms, size_t total_samples);

/* Plot x of sample `i` of an `n`-sample series (n==1 sits at the right edge,
 * matching how the traces are plotted). */
int rd_chart_x(const rd_chart_plan_t* plan, size_t i, size_t n);

/* Epoch ms of sample `i` of an `n`-sample series (0 when no clock). */
uint64_t rd_chart_sample_ms(const rd_chart_plan_t* plan, size_t i, size_t n);

/* Format an epoch-ms instant as local "HH:MM" (tz_offset_min = minutes east of
 * UTC).  Writes "--:--" and returns 0 when the instant is unknown (< 1 s). */
int rd_chart_format_hm(uint64_t epoch_ms, int tz_offset_min, char* out,
                       size_t cap);

/* Fill the x-axis tick labels (left, middle, right) for an `n`-sample series,
 * oldest first.  Empty series yield "--:--". */
void rd_chart_time_labels(const rd_chart_plan_t* plan, size_t n,
                          int tz_offset_min,
                          char out[RD_CHART_TICKS][RD_CHART_LABEL_LEN]);

/* Map a series value onto the plot's y axis for its own extent.
 * Returns plan->y1 for a degenerate (flat) series so the trace sits on the
 * bottom axis instead of dividing by zero. */
int rd_chart_y(const rd_chart_plan_t* plan, bool humidity, uint32_t value);

#ifdef __cplusplus
}
#endif
