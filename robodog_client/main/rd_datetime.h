/*
 * rd_datetime.h -- pure civil-calendar / event-meta formatting for the
 * RoboDog panel rows.
 *
 * The qualifier logic that decides whether an event reads "tomorrow",
 * "MM/DD", "YYYY/MM/DD" or a bare "HH:MM-HH:MM" range is the load-bearing
 * feature of the family-calendar renderer (a wrong date on the family panel
 * is the worst-on-screen failure mode), so these helpers are factored into a
 * dependency-free host-testable unit (stdlib + lma_proto.h only -- no ESP-IDF,
 * no u8g2) and exercised by rd_datetime_selftest.c in the host `cc` gate.
 */
#ifndef RD_DATETIME_H
#define RD_DATETIME_H

#include <stddef.h>
#include <stdint.h>

#include "lma_proto.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Epoch ms (UTC) -> local Y/M/D hh:mm, applying tz_min minutes east of UTC.
 * Civil-from-days conversion, pure arithmetic, no libc tm required. */
void rd_utc_ms_to_dt(uint64_t ms, int tz_min, int *yr, int *mo, int *dy,
                     int *hh, int *mm);

/* Local civil day number for an epoch-ms instant, applying tz_min minutes
 * east of UTC.  SINGLE SOURCE OF TRUTH for the days/tod arithmetic (incl. the
 * tod < 0 underflow fix for negative offsets like EDT -240 min):
 * rd_utc_ms_to_dt() derives its day from this, so the two can never drift
 * apart and an event can never render "today" while the header shows MM/DD. */
int64_t rd_utc_ms_to_local_day(uint64_t ms, int tz_min);

/* "%04d-%02d-%02d  %02d:%02d" into out (cap >= 24). */
void rd_utc_ms_to_datetime(uint64_t ms, int tz_min, char *out, size_t cap);

/* Right-hand meta for an event row: a "HH:MM-HH:MM" time range (ASCII '-',
 * the _tf fonts carry no U+2013 en-dash glyph) prefixed by a date qualifier
 * when the event is not today's civil day: "tomorrow", "MM/DD" for a later
 * day in the same civil year, "YYYY/MM/DD" for a later day in a different
 * year; today renders range-only.  day_now is the display clock's local civil
 * day (0 when the clock is unset -> no qualifier, range only); yr_now is its
 * civil year.  Writes at most 22 chars + NUL (cap >= 32).
 *
 * Branch order is load-bearing: `diff == 1` ("tomorrow") is checked BEFORE
 * `sy == yr_now` so the Dec 31 -> Jan 1 year flip still reads "tomorrow"
 * (today = Dec 31, event = Jan 1 of the next civil year).  The qualifier is
 * keyed off the event START day; a single-day event that starts on a previous
 * day and is still ongoing reads that start day (documented start-day
 * semantics -- revisit only if multi-day events become common). */
void rd_format_event_meta(const lma_cal_event_t *ev, int tz_min,
                          int64_t day_now, int yr_now, char *out, size_t cap);

#ifdef __cplusplus
}
#endif

#endif /* RD_DATETIME_H */
