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
#include "voice_think.h"

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

/* ── think-block stripper ───────────────────────────────────────────────── */

/* The real tag is ASCII "<think>" / "</think>"; built as macros so this file
 * never contains a literal the model could echo. */
#define TAG_OPEN  "<" "think" ">"
#define TAG_CLOSE "<" "/" "think" ">"

static void test_think_clean_text_untouched(void)
{
    char out[256];
    const char *raw = "Okay, twenty-one, twenty-two.";
    CHECK(robodog_think_strip(raw, out, sizeof(out)) > 0,
          "clean text must survive");
    CHECK(strcmp(out, raw) == 0, "clean text unchanged, got %s", out);
}

static void test_think_empty_block_removed(void)
{
    char out[256];
    const char *raw = TAG_OPEN "\n\n" TAG_CLOSE "\n\nOkay, twenty-one.";
    size_t n = robodog_think_strip(raw, out, sizeof(out));
    CHECK(strcmp(out, "Okay, twenty-one.") == 0,
          "empty think block removed, got %s", out);
    CHECK(n == strlen("Okay, twenty-one."), "length matches (%zu)", n);
    CHECK(!robodog_think_present(out), "clean text has no think marker");
}

static void test_think_span_mid_text(void)
{
    char out[256];
    const char *raw = "Sure. " TAG_OPEN "thinking about it" TAG_CLOSE
                      " The answer is 42.";
    robodog_think_strip(raw, out, sizeof(out));
    CHECK(strcmp(out, "Sure. The answer is 42.") == 0,
          "mid-text block removed, got %s", out);
}

static void test_think_unclosed_drops_tail(void)
{
    char out[256];
    const char *raw = "Hello. " TAG_OPEN "still reasoning";
    robodog_think_strip(raw, out, sizeof(out));
    CHECK(strncmp(out, "Hello.", 6) == 0,
          "unclosed block drops the tail, got %s", out);
    CHECK(strstr(out, "still reasoning") == NULL,
          "reasoning text never survives, got %s", out);
}

static void test_think_present(void)
{
    CHECK(robodog_think_present(TAG_OPEN "\n\nhi"), "detects an open tag");
    CHECK(!robodog_think_present("plain text"), "plain text has none");
    CHECK(!robodog_think_present(NULL), "NULL is safe");
}

static void test_think_only_block_yields_empty(void)
{
    char out[64];
    CHECK(robodog_think_strip(TAG_OPEN "\n\n" TAG_CLOSE "\n\n",
                              out, sizeof(out)) == 0,
          "a reply that is only a think block yields empty text");
    CHECK(out[0] == '\0', "and an empty string");
}

static void test_think_split_tag_is_handled(void)
{
    /* The realtime stream splits the tag across deltas; the caller
     * accumulates first, so the joined text must still be stripped. */
    char out[256];
    const char *raw = TAG_OPEN "\n\n" TAG_CLOSE "\n\nDone.";
    robodog_think_strip(raw, out, sizeof(out));
    CHECK(strcmp(out, "Done.") == 0, "split tag handled, got %s", out);
}

static void test_think_partial_tag_is_not_stripped(void)
{
    /* A half-received tag must not eat the answer. */
    char out[256];
    const char *raw = "the thi";
    robodog_think_strip(raw, out, sizeof(out));
    CHECK(strcmp(out, "the thi") == 0, "partial tag kept, got %s", out);
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
    test_think_clean_text_untouched();
    test_think_empty_block_removed();
    test_think_span_mid_text();
    test_think_unclosed_drops_tail();
    test_think_present();
    test_think_only_block_yields_empty();
    test_think_split_tag_is_handled();
    test_think_partial_tag_is_not_stripped();

    /* A capability question must reach the agent that owns the skills/tools;
     * the omni model's generic "I can help with..." answer is not the board's
     * real capability set (and the bench gate's `capabilities` corpus exercises
     * the full-capability path). */
    {
        const char *reason = NULL;
        CHECK(robodog_route_turn("what can you do", &reason) ==
              ROBODOG_ROUTE_AGENT, "capabilities -> agent (reason=%s)",
              reason ? reason : "?");
        CHECK(robodog_route_turn("tell me your capabilities", &reason) ==
              ROBODOG_ROUTE_AGENT, "capabilities (plural) -> agent");
        CHECK(robodog_route_turn("what can you do today", &reason) ==
              ROBODOG_ROUTE_AGENT, "can you do -> agent");
    }

    if (failures != 0) {
        fprintf(stderr, "%d assertion(s) failed\n", failures);
        return 1;
    }
    printf("voice_route + voice_pcm resample: all assertions passed\n");
    return 0;
}
