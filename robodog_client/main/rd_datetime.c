/*
 * rd_datetime.c -- pure civil-calendar / event-meta formatting (see
 * rd_datetime.h).
 *
 * Deliberately dependency-free (stdlib + lma_proto.h only), so it compiles
 * and self-tests on a bare host `cc -std=c11 -Wall -Wextra` as well as inside
 * the ESP-IDF main component.
 */
#include "rd_datetime.h"

#include <stdio.h>

/* Single source of truth for the epoch -> local days/tod arithmetic.  Both
 * rd_utc_ms_to_local_day and rd_utc_ms_to_dt go through this so the civil day
 * an event is diffed against and the civil year/mm/dd rendered for it can
 * never disagree.  tod < 0 compensates negative tz offsets (EDT = -240): an
 * evening-UTC instant that lands on the prior local day must roll the day
 * back, not render a negative clock. */
static void utc_ms_to_day_time(uint64_t ms, int tz_min, int64_t *days_out,
                               int64_t *tod_out)
{
    int64_t s = (int64_t)(ms / 1000) + (int64_t)tz_min * 60;
    int64_t days = s / 86400;
    int64_t tod = s % 86400;
    if (tod < 0) {
        tod += 86400;
        days--;
    }
    *days_out = days;
    *tod_out = tod;
}

int64_t rd_utc_ms_to_local_day(uint64_t ms, int tz_min)
{
    int64_t days, tod;
    utc_ms_to_day_time(ms, tz_min, &days, &tod);
    (void)tod;
    return days;
}

void rd_utc_ms_to_dt(uint64_t ms, int tz_min, int *yr, int *mo, int *dy,
                     int *hh, int *mm)
{
    int64_t days, tod;
    utc_ms_to_day_time(ms, tz_min, &days, &tod);

    /* Civil-from-days (Howard Hinnant days_from_civil inverse). */
    int64_t z = days + 719468;
    int64_t era = (z >= 0 ? z : z - 146096) / 146097;
    unsigned doe = (unsigned)(z - era * 146097);
    unsigned yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    int yy = (int)yoe + (int)(era * 400);
    unsigned doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    unsigned mp = (5 * doy + 2) / 153;
    unsigned dd = doy - (153 * mp + 2) / 5 + 1;
    unsigned moo = mp + (mp < 10 ? 3 : -9);
    yy += (moo <= 2) ? 1 : 0;
    *yr = yy;
    *mo = (int)moo;
    *dy = (int)dd;
    *hh = (int)(tod / 3600);
    *mm = (int)((tod % 3600) / 60);
}

void rd_utc_ms_to_datetime(uint64_t ms, int tz_min, char *out, size_t cap)
{
    int y, mo, d, hh, mm;
    rd_utc_ms_to_dt(ms, tz_min, &y, &mo, &d, &hh, &mm);
    snprintf(out, cap, "%04d-%02d-%02d  %02d:%02d", y, mo, d, hh, mm);
}

void rd_format_event_meta(const lma_cal_event_t *ev, int tz_min,
                          int64_t day_now, int yr_now, char *out, size_t cap)
{
    int sy, sm, sd, sh, smi;
    rd_utc_ms_to_dt(ev->start_ms, tz_min, &sy, &sm, &sd, &sh, &smi);

    char range[16];
    if (ev->end_ms != 0) {
        int ey, em, ed, eh, emi;
        rd_utc_ms_to_dt(ev->end_ms, tz_min, &ey, &em, &ed, &eh, &emi);
        snprintf(range, sizeof(range), "%02d:%02d-%02d:%02d",
                 sh, smi, eh, emi);
    } else {
        snprintf(range, sizeof(range), "%02d:%02d", sh, smi);
    }

    const int64_t day_ev = rd_utc_ms_to_local_day(ev->start_ms, tz_min);
    const int64_t diff = (day_now != 0) ? (day_ev - day_now) : 0;

    if (day_now == 0 || diff == 0) {
        snprintf(out, cap, "%s", range);
    } else if (diff == 1) {
        snprintf(out, cap, "tomorrow %s", range);
    } else if (sy == yr_now) {
        snprintf(out, cap, "%02d/%02d %s", sm, sd, range);
    } else {
        snprintf(out, cap, "%04d/%02d/%02d %s", sy, sm, sd, range);
    }
}
