/*
 * rd_datetime_selftest.c -- host-only self-test for the RoboDog renderer's
 * pure logic extracted into rd_datetime.c + rd_clip.c: the event-row meta
 * qualifier branches ("HH:MM-HH:MM" / "tomorrow" / "MM/DD" / "YYYY/MM/DD"),
 * the local civil-day arithmetic (EDT -240 min underflow), and the measured
 * clip-length silhouette (a clipped title must always fit max_px and never
 * split a UTF-8 sequence).  NOT part of the ESP-IDF build (defines main()).
 *
 * Build & run (from robodog_client/):
 *   cc -std=c11 -Wall -Wextra main/rd_datetime_selftest.c main/rd_datetime.c \
 *      main/rd_clip.c -o /tmp/rd_dt_selftest \
 *      && /tmp/rd_dt_selftest
 */
#include <stdio.h>
#include <string.h>

#include "rd_clip.h"
#include "rd_datetime.h"

/* Fixed reference zone: EDT == UTC-4 == -240 min East of UTC (the firmware's
 * RD_TZ_OFFSET_MINUTES). */
#define TZ_EDT (-240)

#define CHECK(cond)                                                        \
    do {                                                                   \
        if (!(cond)) {                                                     \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
            return 1;                                                      \
        }                                                                  \
        passed++;                                                          \
    } while (0)

/* ---- civil <-> epoch helpers (independent of rd_*; cross-checks them) ---- */

/* Days since 1970-01-01 for a civil y/m/d (Howard Hinnant days_from_civil). */
static int64_t days_from_civil(int y, unsigned m, unsigned d)
{
    y -= (int)(m <= 2);
    const int64_t era = (y >= 0 ? y : y - 399) / 400;
    const unsigned yoe = (unsigned)(y - (int)era * 400);
    const unsigned doy =
        (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
    const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return era * 146097 + (int64_t)doe - 719468;
}

/* Epoch ms of civil y/m/d hh:mm INTERPRETED AS UTC. */
static int64_t to_ms_utc(int y, int mo, int d, int hh, int mm)
{
    const int64_t day = days_from_civil(y, (unsigned)mo, (unsigned)d);
    return (day * 1440 + (int64_t)hh * 60 + mm) * 60000;
}

/* Epoch ms of a civil-local (EDT-4) y/m/d hh:mm: local = UTC + tz, so
 * utc_ms = local_ms - tz*60000 (tz = -240 -> +240 min). */
static int64_t to_ms_local(int y, int mo, int d, int hh, int mm)
{
    return to_ms_utc(y, mo, d, hh, mm) - (int64_t)TZ_EDT * 60000;
}

/* ---- clip test width model: advance = 1 px per BYTE of the prefix --------- */

static int w_bytes(const char *prefix, void *ctx)
{
    (void)ctx;
    return (int)strlen(prefix);
}

/* advance = fixed per-char pixels (ctx = &advance), like a monospace face. */
static int w_fixed(const char *prefix, void *ctx)
{
    const int *adv = (const int *)ctx;
    return (int)strlen(prefix) * *adv;
}

/* 1. The qualifier branches -- CRITICAL regression safety (driving feature). */
static int test_meta_qualifiers(void)
{
    int passed = 0;
    lma_cal_event_t ev = {0};
    char out[40];

    /* Anchors as the firmware computes them, cross-checked against the
     * independent days_from_civil() so the test is not self-referential. */
    const int64_t anchor_local_day = rd_utc_ms_to_local_day(
        to_ms_local(2026, 10, 16, 12, 0), TZ_EDT);
    CHECK(anchor_local_day == days_from_civil(2026, 10, 16));
    const int64_t dec31_local_day = rd_utc_ms_to_local_day(
        to_ms_local(2026, 12, 31, 23, 0), TZ_EDT);
    CHECK(dec31_local_day == days_from_civil(2026, 12, 31));

    /* today -> range-only */
    ev.start_ms = to_ms_local(2026, 10, 16, 9, 0);
    ev.end_ms = to_ms_local(2026, 10, 16, 10, 30);
    rd_format_event_meta(&ev, TZ_EDT, anchor_local_day, 2026,
                         out, sizeof out);
    CHECK(strcmp(out, "09:00-10:30") == 0);

    /* diff == 1 -> "tomorrow" (single-time form; year is the same) */
    memset(&ev, 0, sizeof ev);
    ev.start_ms = to_ms_local(2026, 10, 17, 8, 0);
    rd_format_event_meta(&ev, TZ_EDT, anchor_local_day, 2026,
                         out, sizeof out);
    CHECK(strcmp(out, "tomorrow 08:00") == 0);

    /* ORDERING: "tomorrow" must win over the same-year MM/DD branch -- today
     * Dec 30 2026, event Dec 31 2026 (same civil year, diff == 1). */
    {
        const int64_t d30 = rd_utc_ms_to_local_day(
            to_ms_local(2026, 12, 30, 12, 0), TZ_EDT);
        ev.start_ms = to_ms_local(2026, 12, 31, 9, 0);
        rd_format_event_meta(&ev, TZ_EDT, d30, 2026, out, sizeof out);
        CHECK(strcmp(out, "tomorrow 09:00") == 0);
    }

    /* ORDERING edge: Dec 31 -> Jan 1 crosses into the next civil year;
     * "tomorrow" still must win over the YYYY/MM/DD branch. */
    ev.start_ms = to_ms_local(2027, 1, 1, 0, 30);
    ev.end_ms = to_ms_local(2027, 1, 1, 1, 0);
    rd_format_event_meta(&ev, TZ_EDT, dec31_local_day, 2026,
                         out, sizeof out);
    CHECK(strcmp(out, "tomorrow 00:30-01:00") == 0);

    /* same civil year, later day -> MM/DD */
    memset(&ev, 0, sizeof ev);
    ev.start_ms = to_ms_local(2026, 11, 2, 14, 0);
    rd_format_event_meta(&ev, TZ_EDT, anchor_local_day, 2026,
                         out, sizeof out);
    CHECK(strcmp(out, "11/02 14:00") == 0);

    /* different civil year -> YYYY/MM/DD (and within the documented 22 chars) */
    ev.start_ms = to_ms_local(2027, 1, 5, 9, 0);
    rd_format_event_meta(&ev, TZ_EDT, anchor_local_day, 2026,
                         out, sizeof out);
    CHECK(strcmp(out, "2027/01/05 09:00") == 0);
    CHECK(strlen(out) <= 22);

    /* WIDEST documented output is exactly 22 chars + NUL. */
    {
        const int64_t day_2016 = rd_utc_ms_to_local_day(
            to_ms_local(2016, 1, 1, 12, 0), TZ_EDT);
        ev.start_ms = to_ms_local(2027, 10, 16, 9, 0);
        ev.end_ms = to_ms_local(2027, 10, 16, 10, 0);
        rd_format_event_meta(&ev, TZ_EDT, day_2016, 2016,
                             out, sizeof out);
        CHECK(strcmp(out, "2027/10/16 09:00-10:00") == 0);
        CHECK(strlen(out) == 22);
    }

    /* end_ms == 0 -> single HH:MM (never a bogus 1970 end decode) */
    memset(&ev, 0, sizeof ev);
    ev.start_ms = to_ms_local(2026, 10, 16, 22, 15);
    rd_format_event_meta(&ev, TZ_EDT, anchor_local_day, 2026,
                         out, sizeof out);
    CHECK(strcmp(out, "22:15") == 0);

    /* pre-clock (day_now == 0) -> range-only regardless of the day diff */
    rd_format_event_meta(&ev, TZ_EDT, 0, 0, out, sizeof out);
    CHECK(strcmp(out, "22:15") == 0);

    printf("test_meta_qualifiers: %d checks passed\n", passed);
    return 0;
}

/* 2. Civil-day arithmetic: EDT underflow + boundary exactness + DRY keeps
 *    rd_utc_ms_to_dt and rd_utc_ms_to_local_day in lockstep. */
static int test_local_day_arithmetic(void)
{
    int passed = 0;

    /* 2026-10-16T04:30Z -> 2026-10-16 00:30 local (after local midnight). */
    {
        uint64_t ms = (uint64_t)to_ms_utc(2026, 10, 16, 4, 30);
        CHECK(rd_utc_ms_to_local_day(ms, TZ_EDT) ==
              days_from_civil(2026, 10, 16));
        int y, mo, d, hh, mm;
        rd_utc_ms_to_dt(ms, TZ_EDT, &y, &mo, &d, &hh, &mm);
        CHECK(y == 2026 && mo == 10 && d == 16 && hh == 0 && mm == 30);
    }

    /* 2026-10-16T02:00Z -> local 2026-10-15 22:00: the tod<0 underflow case
     * (02:00 - 4h is negative) must roll BACK, not wrap forward. */
    {
        uint64_t ms = (uint64_t)to_ms_utc(2026, 10, 16, 2, 0);
        CHECK(rd_utc_ms_to_local_day(ms, TZ_EDT) ==
              days_from_civil(2026, 10, 15));
        int y, mo, d, hh, mm;
        rd_utc_ms_to_dt(ms, TZ_EDT, &y, &mo, &d, &hh, &mm);
        CHECK(y == 2026 && mo == 10 && d == 15 && hh == 22 && mm == 0);
    }

    /* Day-boundary exactness around the local (EDT) midnight. */
    {
        /* 2026-10-17T04:00Z is exactly 2026-10-17 00:00 EDT. */
        uint64_t ms = (uint64_t)to_ms_utc(2026, 10, 17, 4, 0);
        CHECK(rd_utc_ms_to_local_day(ms, TZ_EDT) ==
              days_from_civil(2026, 10, 17));
        /* ...and one minute earlier is still the prior civil day. */
        ms = (uint64_t)to_ms_utc(2026, 10, 17, 3, 59);
        CHECK(rd_utc_ms_to_local_day(ms, TZ_EDT) ==
              days_from_civil(2026, 10, 16));
    }

    /* Cross-year evening local (still prior-day in UTC terms) keeps the day. */
    {
        uint64_t ms = (uint64_t)to_ms_utc(2027, 1, 1, 4, 30);
        CHECK(rd_utc_ms_to_local_day(ms, TZ_EDT) ==
              days_from_civil(2027, 1, 1));
        int y, mo, d, hh, mm;
        rd_utc_ms_to_dt(ms, TZ_EDT, &y, &mo, &d, &hh, &mm);
        CHECK(y == 2027 && mo == 1 && d == 1 && hh == 0 && mm == 30);
    }

    /* DRY / lockstep: for a sweep of instants the civil day that rd_utc_ms_to_dt
     * yields must equal rd_utc_ms_to_local_day (they share one arithmetic). */
    {
        static const int sweep[][5] = {
            {2026, 10, 16, 0, 0},  {2026, 10, 16, 23, 59},
            {2026, 12, 31, 23, 59}, {2027, 1, 1, 0, 0},
            {2027, 1, 1, 4, 1},    {2027, 7, 4, 20, 15},
            {2024, 2, 29, 12, 0},  {2038, 1, 19, 3, 14},
            {1970, 1, 1, 4, 0},    {1969, 12, 31, 20, 0},
        };
        for (size_t i = 0; i < sizeof(sweep) / sizeof(sweep[0]); i++) {
            const int y = sweep[i][0], mo = sweep[i][1], d = sweep[i][2];
            const int hh = sweep[i][3], mm = sweep[i][4];
            uint64_t ms = (uint64_t)to_ms_utc(y, mo, d, hh, mm);
            int y2, mo2, d2, hh2, mm2;
            rd_utc_ms_to_dt(ms, TZ_EDT, &y2, &mo2, &d2, &hh2, &mm2);
            CHECK(days_from_civil(y2, (unsigned)mo2, (unsigned)d2) ==
                  rd_utc_ms_to_local_day(ms, TZ_EDT));
        }
    }

    /* Header datetime formatter spot-check. */
    {
        char line[40];
        rd_utc_ms_to_datetime((uint64_t)to_ms_local(2026, 10, 16, 9, 5),
                              TZ_EDT, line, sizeof line);
        CHECK(strcmp(line, "2026-10-16  09:05") == 0);
    }

    printf("test_local_day_arithmetic: %d checks passed\n", passed);
    return 0;
}

/* 3. Clip silhouette: rd_clip_prefix must always fit max_px, handle the
 *    boundary budgets (0 / 1 / 2 / exact-fit / negative), and never split a
 *    UTF-8 sequence. */
static int test_clip_prefix(void)
{
    int passed = 0;
    int adv10 = 10;

    size_t lo;

    /* exact-fit: 3 bytes + 3px ellipsis == the 6px budget. */
    lo = rd_clip_prefix("abcdefgh", 8, 6, 3, w_bytes, NULL);
    CHECK(lo == 3);
    CHECK((int)lo + 3 <= 6);

    /* tighter budget leaves one glyph. */
    lo = rd_clip_prefix("abcdefgh", 8, 5, 3, w_bytes, NULL);
    CHECK(lo == 2);

    /* nothing fits (single glyph + ellipsis overflow the budget). */
    lo = rd_clip_prefix("abcdefgh", 8, 3, 3, w_bytes, NULL);
    CHECK(lo == 0);

    /* zero / negative budgets fail closed (never return a full length that
     * would let the caller draw the untruncated title). */
    CHECK(rd_clip_prefix("abcdefgh", 8, 0, 3, w_bytes, NULL) == 0);
    CHECK(rd_clip_prefix("abcdefgh", 8, -5, 3, w_bytes, NULL) == 0);
    CHECK(rd_clip_prefix("abcdefgh", 0, 100, 3, w_bytes, NULL) == 0);
    CHECK(rd_clip_prefix("abcdefgh", 8, 100, -1, w_bytes, NULL) == 0);

    /* UTF-8 safety: bytes 0xC3 0xA9 = "é" must never be split; with the
     * every-byte-costs-1px model and ell_w=1: "ab" fits in 3px (2+1), "abé"
     * (3 bytes) does not (3+1 > 3), and at 5px whole "abé" fits (4+1). */
    {
        const char *t8 = "ab\xC3\xA9" "d";
        CHECK(strlen(t8) == 5);
        lo = rd_clip_prefix(t8, 5, 3, 1, w_bytes, NULL);
        CHECK(lo == 2);
        lo = rd_clip_prefix(t8, 5, 4, 1, w_bytes, NULL);
        CHECK(lo == 2);                     /* 3 (a,b,0xC3) would split é -> 2 */
        lo = rd_clip_prefix(t8, 5, 5, 1, w_bytes, NULL);
        CHECK(lo == 4);                     /* whole é + room: no split */
        /* verify the returned boundary is UTF-8-valid: the next byte from the
         * boundary is not a continuation byte. */
        CHECK(((unsigned char)t8[lo] & 0xC0) != 0x80);
    }

    /* fixed-advance (monospace-ish) model: silhouette always fits max_px. */
    lo = rd_clip_prefix("abcde", 5, 30, 10, w_fixed, &adv10);
    CHECK(lo == 2);                     /* 2*10 + 10 == 30: exact fit */
    lo = rd_clip_prefix("abcde", 5, 29, 10, w_fixed, &adv10);
    CHECK(lo == 1);                     /* 10 + 10 == 20 <= 29 */

    /* whole title fits alongside the ellipsis -> n returned. */
    lo = rd_clip_prefix("abcdefgh", 8, 100, 3, w_bytes, NULL);
    CHECK(lo == 8);

    /* silhouette property sweep: every returned length fits its budget. */
    {
        const char *t = "hey \xC3\xA9t\xC3\xA9 d\xC3\xBC" "de tails";
        size_t n = strlen(t);
        for (int budget = -2; budget <= (int)n * 4; budget++) {
            lo = rd_clip_prefix(t, n, budget, 5, w_bytes, NULL);
            CHECK(lo <= n);
            CHECK(lo == 0 || (int)lo + 5 <= budget);
            if (lo > 0) {
                CHECK(((unsigned char)t[lo] & 0xC0) != 0x80);
            }
        }
    }

    printf("test_clip_prefix: %d checks passed\n", passed);
    return 0;
}

/* 4. Greedy first-fit word wrap: rd_wrap_lines must split a title into
 *    contiguous-budget lines (each fitted line's measured width <= max_px),
 *    hard-break an over-long token into glyph-aligned chunks, never split a
 *    UTF-8 sequence, trim leading/trailing space runs, and fold runs of
 *    spaces into a single logical gap. */
static int test_word_wrap(void)
{
    int passed = 0;
    rd_wrap_line_t ln[RD_WRAP_MAX_LINES];
    size_t nl;

    /* 1. short title -> a single whole line, off=0 len=n. */
    nl = rd_wrap_lines("Peck", 4, 218, w_bytes, NULL, ln, 8);
    CHECK(nl == 1);
    CHECK(ln[0].off == 0);
    CHECK(ln[0].len == 4);

    /* 2. greedy multi-token wrap (byte-width model): exact line boundaries. */
    /*    "one two three" at 7px -> "one two" (7) then "three" (5). */
    nl = rd_wrap_lines("one two three", 13, 7, w_bytes, NULL, ln, 8);
    CHECK(nl == 2);
    CHECK(ln[0].off == 0 && ln[0].len == 7);
    CHECK(ln[1].off == 8 && ln[1].len == 5);
    CHECK(ln[0].len <= 7 && ln[1].len <= 7);

    /*    a token exactly filling max_px lets the NEXT token start a line. */
    nl = rd_wrap_lines("aa bbbb cc", 10, 5, w_bytes, NULL, ln, 8);
    CHECK(nl == 3);                       /* "aa" | "bbbb" | "cc" */
    CHECK(ln[0].len == 2 && ln[1].len == 4 && ln[2].len == 2);
    CHECK(ln[0].len <= 5 && ln[1].len <= 5 && ln[2].len <= 5);

    /* 3. a single word wider than max_px -> hard char-break; every chunk
     *    fits max_px and the chunks tile the word byte-for-byte. */
    nl = rd_wrap_lines("abcdefgh", 8, 3, w_bytes, NULL, ln, 8);
    CHECK(nl == 3);                       /* abc | def | gh */
    CHECK(ln[0].off == 0 && ln[0].len == 3);
    CHECK(ln[1].off == 3 && ln[1].len == 3);
    CHECK(ln[2].off == 6 && ln[2].len == 2);
    CHECK(ln[0].len <= 3 && ln[1].len <= 3 && ln[2].len <= 3);
    /* contiguous tiling: no gaps or overlap across the word */
    CHECK(ln[0].off + ln[0].len == ln[1].off);
    CHECK(ln[1].off + ln[1].len == ln[2].off);

    /* 4. no-space title hard-break (same path as #3). */
    nl = rd_wrap_lines("abcdef", 6, 2, w_bytes, NULL, ln, 8);
    CHECK(nl == 3);                       /* ab | cd | ef */
    CHECK(ln[0].len == 2 && ln[1].len == 2 && ln[2].len == 2);

    /* 5. UTF-8 safety: "abéd" must never split the é (C3 A9).
     *    At 2px: "ab" | "é" | "d" -- the é stays whole. */
    {
        const char *t8 = "ab\xC3\xA9" "d";      /* abéd == a b é d */
        nl = rd_wrap_lines(t8, 5, 2, w_bytes, NULL, ln, 8);
        CHECK(nl == 3);
        CHECK(ln[0].off == 0 && ln[0].len == 2);
        CHECK(ln[1].off == 2 && ln[1].len == 2);   /* all é bytes together */
        CHECK(ln[2].off == 4 && ln[2].len == 1);
    }

    /* 6. fail closed: max_px <= 0, n == 0, n > RD_CLIP_TITLE_MAX -> 0 lines. */
    CHECK(rd_wrap_lines("abc", 3, 0, w_bytes, NULL, ln, 8) == 0);
    CHECK(rd_wrap_lines("abc", 3, -5, w_bytes, NULL, ln, 8) == 0);
    CHECK(rd_wrap_lines("abc", 0, 100, w_bytes, NULL, ln, 8) == 0);
    {
        char long_t[RD_CLIP_TITLE_MAX + 2];
        memset(long_t, 'x', sizeof long_t - 1);
        long_t[sizeof long_t - 1] = '\0';
        CHECK(rd_wrap_lines(long_t, RD_CLIP_TITLE_MAX + 1, 218,
                            w_bytes, NULL, ln, 8) == 0);
    }

    /* 7. property sweep: for a mixed-ASCII/UTF-8 title and a sweep of
     *    budgets (>= the widest 2-byte glyph, so even a hard-broken
     *    single-glyph line fits literally), every returned line's measured
     *    width <= budget and every line stays UTF-8-safe. */
    {
        const char *t = "hey \xC3\xA9t\xC3\xA9 d\xC3\xBC" "de tails";
        const size_t n = strlen(t);
        for (int budget = 2; budget <= (int)n * 4; budget++) {
            nl = rd_wrap_lines(t, n, budget, w_bytes, NULL, ln, 8);
            CHECK(nl >= 1);
            /* wide budget collapses the title to a single whole line */
            if (budget >= (int)n) {
                CHECK(nl == 1);
                CHECK(ln[0].off == 0 && ln[0].len == n);
            }
            for (size_t k = 0; k < nl && k < RD_WRAP_MAX_LINES; k++) {
                CHECK(ln[k].len > 0);
                CHECK(ln[k].off + ln[k].len <= n);
                CHECK((int)ln[k].len <= budget);          /* width fits */
                CHECK(((unsigned char)t[ln[k].off] & 0xC0) != 0x80);        /* start */
                CHECK(((unsigned char)t[ln[k].off + ln[k].len] & 0xC0) != 0x80); /* end */
            }
        }
    }

    /* out_cap bound: function returns the TRUE line count even when it
     * exceeds out_cap; only the first out_cap entries are written. */
    {
        rd_wrap_line_t capped[2];
        capped[0].off = capped[0].len = 99;
        capped[1].off = capped[1].len = 99;
        nl = rd_wrap_lines("abcdefghij", 10, 2, w_bytes, NULL, capped, 2);
        CHECK(nl == 5);                    /* ab cd ef gh ij */
        CHECK(nl > 2);
        CHECK(capped[0].off == 0 && capped[0].len == 2);
        CHECK(capped[1].off == 2 && capped[1].len == 2);
        /* out stays untouched beyond out_cap (no overflow). */
    }

    /* 8. interior runs of spaces fold into ONE logical gap: both words join
     *    a single line carrying the original spacing, and when the budget is
     *    tight the split lands at the space boundary with no dangling
     *    leading/trailing spaces on either line. */
    {
        const char *s = "aa    bb";
        /* big budget: one line spanning both tokens (original spacing kept) */
        nl = rd_wrap_lines(s, 8, 10, w_bytes, NULL, ln, 8);
        CHECK(nl == 1);
        CHECK(ln[0].off == 0 && ln[0].len == 8);   /* "aa    bb" == 8 bytes */
        /* tight budget: "aa" then "bb" -- line 1 never ends with spaces */
        nl = rd_wrap_lines(s, 8, 5, w_bytes, NULL, ln, 8);
        CHECK(nl == 2);
        CHECK(ln[0].off == 0 && ln[0].len == 2);
        CHECK(ln[1].off == 6 && ln[1].len == 2);
        /* line 1's last byte is not a space (no dangling separator) */
        CHECK(s[ln[0].off + ln[0].len - 1] != ' ');
    }

    /* leading/trailing space runs are trimmed off the lines. */
    {
        const char *s = "  lead trail  ";
        nl = rd_wrap_lines(s, 14, 100, w_bytes, NULL, ln, 8);
        CHECK(nl == 1);
        CHECK(s[ln[0].off] != ' ');
        CHECK(s[ln[0].off + ln[0].len - 1] != ' ');
        CHECK(strncmp(s + ln[0].off, "lead trail", ln[0].len) == 0);
    }

    printf("test_word_wrap: %d checks passed\n", passed);
    return 0;
}

int main(void)
{
    printf("rd_datetime self-test\n");
    if (test_meta_qualifiers() != 0 ||
        test_local_day_arithmetic() != 0 ||
        test_clip_prefix() != 0 ||
        test_word_wrap() != 0) {
        printf("RESULT: FAIL\n");
        return 1;
    }
    printf("RESULT: PASS\n");
    return 0;
}
