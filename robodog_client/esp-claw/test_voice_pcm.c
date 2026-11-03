/*
 * RoboDog ESP-Claw host unit tests for the PCM silence trimmer.
 *
 * Same harness as test_voice_split.c (shared s_failures counter + the single
 * main() there), compiled against the EXACT voice_pcm.c the firmware runs.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "voice_pcm.h"

extern int s_failures;

#define CHECK(cond, fmt, ...)                                                  \
    do {                                                                       \
        if (!(cond)) {                                                         \
            s_failures++;                                                      \
            fprintf(stderr, "FAIL %s:%d: " fmt "\n", __func__, __LINE__,       \
                    ##__VA_ARGS__);                                            \
        }                                                                      \
    } while (0)

#define RATE 24000u

/* Build [silence lead_ms][speech dur_ms][silence tail_ms] and return the
 * window the trimmer chose. */
static void build(int16_t *buf, size_t lead_ms, size_t speech_ms,
                  size_t tail_ms, size_t *n_out)
{
    const size_t lead = RATE * lead_ms / 1000;
    const size_t speech = RATE * speech_ms / 1000;
    const size_t tail = RATE * tail_ms / 1000;
    memset(buf, 0, lead * sizeof(int16_t));
    for (size_t i = 0; i < speech; i++) {
        buf[lead + i] = (int16_t)((i % 2) ? 8000 : -8000); /* loud alternating */
    }
    memset(buf + lead + speech, 0, tail * sizeof(int16_t));
    *n_out = lead + speech + tail;
}

/* ── Leading silence is removed, trailing kept to the budget ───────────── */
void test_pcm_trim_basic(void)
{
    static int16_t buf[RATE * 2];
    size_t n = 0, off = 0, kept = 0;
    build(buf, 300, 500, 400, &n);
    robodog_pcm_trim_silence(buf, n, RATE, 0, 50, &off, &kept);
    const size_t lead = RATE * 300 / 1000;
    const size_t speech = RATE * 500 / 1000;
    const size_t tail_keep = RATE * 50 / 1000;
    CHECK(off == lead, "lead 300ms trimmed entirely (off=%zu, want %zu)", off,
          lead);
    CHECK(kept == speech + tail_keep,
          "kept = speech + 50ms tail (got %zu, want %zu)", kept,
          speech + tail_keep);
    CHECK(buf[off] != 0, "the window starts on speech");
}

/* ── A lead_keep budget keeps that much lead-in (turn's first chunk) ────── */
void test_pcm_trim_lead_budget(void)
{
    static int16_t buf[RATE * 2];
    size_t n = 0, off = 0, kept = 0;
    build(buf, 300, 400, 200, &n);
    robodog_pcm_trim_silence(buf, n, RATE, 120, 80, &off, &kept);
    const size_t lead = RATE * 300 / 1000;
    const size_t keep = RATE * 120 / 1000;
    CHECK(off == lead - keep, "120ms of the lead-in kept (off=%zu, want %zu)",
          off, lead - keep);
}

/* ── Inter-word dips must NOT end the trim (hold window) ───────────────── */
void test_pcm_trim_hold_window(void)
{
    static int16_t buf[20000];
    size_t n = 0;
    /* 100 ms speech, a 10 ms silent dip, 100 ms speech, 100 ms tail. */
    build(buf, 100, 100, 0, &n);
    const size_t lead = RATE * 100 / 1000;
    const size_t speech = RATE * 100 / 1000;
    memset(buf + lead + speech, 0, RATE * 10 / 1000 * sizeof(int16_t));
    const size_t dip = RATE * 10 / 1000;
    for (size_t i = 0; i < speech; i++) {
        buf[lead + speech + dip + i] = (int16_t)((i % 2) ? 8000 : -8000);
    }
    memset(buf + lead + speech + dip + speech, 0,
           RATE * 100 / 1000 * sizeof(int16_t));
    n = lead + speech + dip + speech + RATE * 100 / 1000;

    size_t off = 0, kept = 0;
    robodog_pcm_trim_silence(buf, n, RATE, 0, 0, &off, &kept);
    CHECK(off == lead, "lead trimmed to the first speech run");
    CHECK(kept == speech + dip + speech,
          "the 10ms dip is INSIDE the kept window (kept=%zu, want %zu)", kept,
          speech + dip + speech);
}

/* ── Silent / degenerate input is returned unchanged (never empty) ─────── */
void test_pcm_trim_degenerate(void)
{
    static int16_t buf[1000];
    memset(buf, 0, sizeof(buf));
    size_t off = 9, kept = 9;
    robodog_pcm_trim_silence(buf, 1000, RATE, 0, 0, &off, &kept);
    CHECK(off == 0 && kept == 1000, "all-silent chunk returned unchanged");

    /* A chunk shorter than the hold window (a click) is left alone. */
    buf[5] = 12000;
    robodog_pcm_trim_silence(buf, 1000, RATE, 0, 0, &off, &kept);
    CHECK(off == 0 && kept == 1000, "sub-hold blip never trims the chunk");

    /* NULL / zero-length are safe no-ops. */
    off = 1;
    kept = 1;
    robodog_pcm_trim_silence(NULL, 10, RATE, 0, 0, &off, &kept);
    CHECK(off == 0 && kept == 10, "NULL pcm leaves the window unchanged");
    robodog_pcm_trim_silence(buf, 0, RATE, 0, 0, &off, &kept);
    CHECK(off == 0 && kept == 0, "zero-length chunk is a no-op");
}
