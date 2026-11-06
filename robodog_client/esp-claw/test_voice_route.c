/*
 * Host test for the Phase 3 voice-turn capability router (voice_route.c).
 *
 * The router decides whether a turn goes to the omni model (fast, no tools) or
 * to claw_core (skills/MCP/web, slower).  A false OMNI verdict silently loses
 * capability — the board answers "search the web for X" from model memory
 * instead of searching — so the routing contract is pinned here rather than
 * eyeballed on hardware.
 *
 * Also pins robodog_pcm_resample (voice_pcm.c): the omni endpoint declares its
 * output rate per audio delta, and a wrong rate assumption is what once made a
 * 16 kHz WAV play slow with very low pitch.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "voice_pcm.h"
#include "voice_route.h"

static int failures;

#define CHECK(cond, ...)                                                      \
    do {                                                                      \
        if (!(cond)) {                                                        \
            fprintf(stderr, "FAIL %s:%d: ", __FILE__, __LINE__);              \
            fprintf(stderr, __VA_ARGS__);                                     \
            fprintf(stderr, "\n");                                            \
            failures++;                                                       \
        }                                                                     \
    } while (0)

static void test_capability_turns_route_to_agent(void)
{
    static const char *const texts[] = {
        "search the web for the ISS orbit",
        "Google the weather in Taipei",
        "what's on my calendar tomorrow",
        "remind me in ten minutes",
        "write a file called notes.txt",
        "run the build",
        "turn on the light",
        "what is the temperature in the living room",
        "remember that I like tea",
        "send an email to mum",
        "查一下天氣",
        "開燈",
    };
    for (size_t i = 0; i < sizeof(texts) / sizeof(texts[0]); i++) {
        const char *reason = NULL;
        const robodog_route_t r = robodog_route_turn(texts[i], &reason);
        CHECK(r == ROBODOG_ROUTE_AGENT, "%s should route to AGENT (got OMNI)",
              texts[i]);
        CHECK(reason != NULL && strcmp(reason, "default") != 0,
              "%s should report a keyword reason", texts[i]);
    }
}

static void test_small_talk_routes_to_omni(void)
{
    static const char *const texts[] = {
        "hello",
        "how are you today",
        "tell me a joke",
        "what time is it",
        "count slowly from twenty to thirty",
        "listen to me",          /* must not match the "list " keyword */
        "who is the president of France",
    };
    for (size_t i = 0; i < sizeof(texts) / sizeof(texts[0]); i++) {
        const char *reason = NULL;
        const robodog_route_t r = robodog_route_turn(texts[i], &reason);
        CHECK(r == ROBODOG_ROUTE_OMNI, "%s should route to OMNI (got AGENT)",
              texts[i]);
        CHECK(reason != NULL && strcmp(reason, "default") == 0,
              "%s should report reason=default", texts[i]);
    }
}

static void test_empty_and_null(void)
{
    const char *reason = NULL;
    CHECK(robodog_route_turn("", &reason) == ROBODOG_ROUTE_OMNI,
          "empty text must stay on the fast path");
    CHECK(reason != NULL && strcmp(reason, "empty") == 0,
          "empty text reports reason=empty");
    CHECK(robodog_route_turn(NULL, &reason) == ROBODOG_ROUTE_OMNI,
          "NULL text must not crash and must stay OMNI");
}

static void test_case_insensitive(void)
{
    const char *reason = NULL;
    CHECK(robodog_route_turn("SEARCH for cats", &reason) == ROBODOG_ROUTE_AGENT,
          "uppercase keyword must match");
    CHECK(robodog_route_turn("Search for cats", &reason) == ROBODOG_ROUTE_AGENT,
          "mixed-case keyword must match");
    CHECK(robodog_route_turn("sEaRcH for cats", &reason) == ROBODOG_ROUTE_AGENT,
          "any-case keyword must match");
}

static void test_keyword_table_is_wellformed(void)
{
    const size_t n = robodog_route_keyword_count();
    CHECK(n > 10, "keyword table looks truncated (%zu entries)", n);
    for (size_t i = 0; i < n; i++) {
        const char *kw = robodog_route_keyword(i);
        CHECK(kw != NULL && kw[0] != '\0', "keyword %zu is empty", i);
    }
    CHECK(robodog_route_keyword(n) == NULL, "table must end at count");
}

/* ── resampler ──────────────────────────────────────────────────────────── */

static void test_resample_same_rate_is_copy(void)
{
    int16_t in[8] = { 1, 2, 3, 4, 5, 6, 7, 8 };
    int16_t out[8] = { 0 };
    const size_t n = robodog_pcm_resample(in, 8, out, 8, 24000, 24000);
    CHECK(n == 8, "same-rate must copy every sample (got %zu)", n);
    CHECK(memcmp(in, out, sizeof(in)) == 0, "same-rate copy must be exact");
}

static void test_resample_zero_rate_is_copy(void)
{
    int16_t in[4] = { 10, 20, 30, 40 };
    int16_t out[4] = { 0 };
    const size_t n = robodog_pcm_resample(in, 4, out, 4, 0, 24000);
    CHECK(n == 4, "rate 0 means 'unknown', copy (got %zu)", n);
    CHECK(memcmp(in, out, sizeof(in)) == 0, "rate-0 copy must be exact");
}

static void test_resample_upsamples(void)
{
    /* 16 kHz -> 24 kHz is 1.5x.  100 input samples must yield ~150. */
    int16_t in[100];
    for (size_t i = 0; i < 100; i++) {
        in[i] = (int16_t)(i * 10);
    }
    int16_t out[256];
    const size_t n = robodog_pcm_resample(in, 100, out, 256, 16000, 24000);
    CHECK(n >= 148 && n <= 150, "16k->24k should yield ~150 samples (got %zu)",
          n);
    /* Monotone input must stay monotone (no wrap/aliasing). */
    for (size_t i = 1; i < n; i++) {
        CHECK(out[i] >= out[i - 1], "resampled output must stay monotone");
    }
}

static void test_resample_downsample_bounded(void)
{
    int16_t in[1000];
    memset(in, 0, sizeof(in));
    int16_t out[64];
    const size_t n = robodog_pcm_resample(in, 1000, out, 64, 24000, 16000);
    CHECK(n <= 64, "resampler must respect out_cap (got %zu)", n);
}

static void test_resample_rejects_null(void)
{
    int16_t out[8];
    CHECK(robodog_pcm_resample(NULL, 8, out, 8, 16000, 24000) == 0,
          "NULL input must yield 0");
    int16_t in[8] = { 0 };
    CHECK(robodog_pcm_resample(in, 8, NULL, 8, 16000, 24000) == 0,
          "NULL output must yield 0");
    CHECK(robodog_pcm_resample(in, 8, out, 0, 16000, 24000) == 0,
          "zero capacity must yield 0");
}

int main(void)
{
    test_capability_turns_route_to_agent();
    test_small_talk_routes_to_omni();
    test_empty_and_null();
    test_case_insensitive();
    test_keyword_table_is_wellformed();
    test_resample_same_rate_is_copy();
    test_resample_zero_rate_is_copy();
    test_resample_upsamples();
    test_resample_downsample_bounded();
    test_resample_rejects_null();

    if (failures != 0) {
        fprintf(stderr, "%d assertion(s) failed\n", failures);
        return 1;
    }
    printf("voice_route + voice_pcm resample: all assertions passed\n");
    return 0;
}
