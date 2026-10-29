/*
 * RoboDog ESP-Claw host unit tests for the pure-C voice modules
 * (R6: whole-reply chunk splitter lossless/ordered/<=cap;
 *  R7: transcript join + drop-oldest cap-keep).
 *
 * These compile the EXACT source the device firmware builds (voice_split.c /
 * voice_transcript.c) under Bazel as `//robodog_client:test_voice_split`, so
 * the invariants the robodog-claw-voice-verify gate relies on (and the
 * display/audio sync) are proven on the host — a mid-reply drop or a
 * display-accumulation regression can never sail through with green hardware
 * gates.
 *
 * No test framework: plain asserts + a failing rank, so it runs in any Bazel
 * cc_test sandbox with zero deps.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "voice_split.h"
#include "voice_transcript.h"
#include "voice_md.h"

/* ── tiny harness ───────────────────────────────────────────────────────── */
/* Non-static so test_voice_lookahead.c (R9 double-buffer exchange) asserts
 * against the SAME counter and the shared main() reflects its failures. */
int s_failures = 0;

#define CHECK(cond, fmt, ...)                                                  \
    do {                                                                       \
        if (!(cond)) {                                                         \
            s_failures++;                                                      \
            fprintf(stderr, "FAIL %s:%d: " fmt "\n", __func__, __LINE__,       \
                    ##__VA_ARGS__);                                            \
        }                                                                      \
    } while (0)

/* Remove ALL whitespace (space/tab/newline): the splitter intentionally drops
 * seam whitespace (it strips each chunk's trailing ws and skips the next
 * window's leading ws), so lossless content comparison is modulo whitespace. */
static char *strip_ws(char *dst, size_t dst_cap, const char *s)
{
    size_t d = 0;
    for (const char *p = s; *p != '\0' && d + 1 < dst_cap; p++) {
        if (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') {
            continue;
        }
        dst[d++] = *p;
    }
    dst[d] = '\0';
    return dst;
}

/* Split into chunks and rebuild them in order (single space seams) so the
 * lossless/order/<=cap invariants can be checked. */
static size_t split_all(const char *reply, size_t cap, char *arena,
                        size_t arena_cap, size_t *offsets, size_t max_chunks,
                        bool *truncated, char *rebuild, size_t rebuild_cap)
{
    const size_t n = robodog_voice_split_reply(reply, cap, arena, arena_cap,
                                               offsets, max_chunks, truncated);
    memset(rebuild, 0, rebuild_cap);
    for (size_t i = 0; i < n; i++) {
        if (i > 0 && strlen(rebuild) + 1 < rebuild_cap) {
            strcat(rebuild, " ");
        }
        strncat(rebuild, arena + offsets[i], rebuild_cap - strlen(rebuild) - 1);
    }
    return n;
}

/* ── R6: voice_split_reply ──────────────────────────────────────────────── */

static void test_splitter_lossless_ordered(void)
{
    char arena[4096];
    size_t offsets[128];
    char rebuild[2048], a[2048], b[2048];
    bool truncated = false;

    /* Verbose multi-paragraph reply with mixed sentence breaks, commas,
     * newlines and space-seam edge cases. */
    const char *reply =
        "Here is what I can do. I can tell you the time, the weather,\n"
        "or a joke.\n\n"
        "I also control the smart irrigation system!\n\n"
        "My capabilities include turning on the pump, checking the soil "
        "moisture, and reporting sensor values. Would you like me to start?"
        "  With two spaces after a sentence.  And a tab\tbetween words.";
    const size_t cap = 40;
    const size_t n = split_all(reply, cap, arena, sizeof(arena), offsets,
                               64, &truncated, rebuild, sizeof(rebuild));
    CHECK(!truncated, "cap=40 over a ~450-char reply must not truncate "
                      "(n=%zu)", n);
    CHECK(n > 1, "expected multiple chunks, got %zu", n);

    strip_ws(a, sizeof(a), reply);
    strip_ws(b, sizeof(b), rebuild);
    CHECK(strcmp(a, b) == 0, "lossless/order broken:\n  reply  =%s\n  rebuild=%s",
          a, b);

    for (size_t i = 0; i < n; i++) {
        const char *ch = arena + offsets[i];
        const size_t l = strlen(ch);
        CHECK(l >= 1, "chunk %zu is empty", i);
        CHECK(l <= cap, "chunk %zu is %zu chars (cap=%zu)", i, l, cap);
    }
}

static void test_splitter_edge_cases(void)
{
    char arena[4096];
    size_t offsets[128];
    char rebuild[2048], a[2048], b[2048];
    bool truncated = false;

    /* Break char exactly at the cap: the cut must stay in-bounds and the split
     * stays lossless (the clamp keeps the boundary break char out of the too-
     * long chunk; it becomes a short trailing chunk). */
    {
        char reply[80];
        memset(reply, 'a', 40);
        reply[40] = '.';
        reply[41] = '\0';
        const size_t n = split_all(reply, 40, arena, sizeof(arena), offsets,
                                   64, &truncated, rebuild, sizeof(rebuild));
        CHECK(!truncated, "break-at-cap must not be flagged truncated");
        CHECK(n >= 1, "break-at-cap must produce at least one chunk");
        for (size_t i = 0; i < n; i++) {
            CHECK(strlen(arena + offsets[i]) <= 40,
                  "chunk %zu exceeds the cap (40)", i);
        }
        strip_ws(a, sizeof(a), reply);
        strip_ws(b, sizeof(b), rebuild);
        CHECK(strcmp(a, b) == 0, "break-at-cap must be lossless");
    }

    /* A long window with NO break/space: hard cutoff allowed. */
    {
        char reply[200];
        memset(reply, 'x', 100);
        reply[100] = '\0';
        const size_t n = split_all(reply, 30, arena, sizeof(arena), offsets,
                                   64, &truncated, rebuild, sizeof(rebuild));
        CHECK(!truncated && n == 4, "hard-cutoff x*100 with cap=30: 4 chunks, "
              "got %zu (truncated=%d)", n, (int)truncated);
        CHECK(strlen(arena + offsets[3]) == 10,
              "last hard-cutoff chunk is 10 chars, got %zu",
              strlen(arena + offsets[3]));
        strip_ws(a, sizeof(a), reply);
        strip_ws(b, sizeof(b), rebuild);
        CHECK(strcmp(a, b) == 0, "hard-cutoff must be lossless");
    }

    /* Newline runs: blank lines collapse, chunks never start blank.  Use a
     * small cap so the reply genuinely spans multiple chunks. */
    {
        const char *reply =
            "Here.\n\n\nThere.\n\nEverywhere.\nAnd back again.\nDone.";
        const size_t n = split_all(reply, 12, arena, sizeof(arena), offsets,
                                   64, &truncated, rebuild, sizeof(rebuild));
        CHECK(n >= 3, "newline runs with cap=12 span multiple chunks (got %zu)",
              n);
        for (size_t i = 0; i < n; i++) {
            CHECK(arena[offsets[i]] != ' ' && arena[offsets[i]] != '\n',
                  "chunk %zu starts blank", i);
        }
        strip_ws(a, sizeof(a), reply);
        strip_ws(b, sizeof(b), rebuild);
        CHECK(strcmp(a, b) == 0, "newline-run split must be lossless");
    }

    /* Empty reply: 0 chunks, not truncated. */
    {
        const size_t n = split_all("", 40, arena, sizeof(arena), offsets, 64,
                                   &truncated, rebuild, sizeof(rebuild));
        CHECK(n == 0 && !truncated, "empty reply: 0 chunks, got %zu", n);
    }
}

static void test_splitter_truncation_flag(void)
{
    /* Over-cap replies MUST report the dropped tail so the device ends in the
     * honest "truncated" state and the gate FAILs (R3). */
    char arena[4096];
    size_t offsets[128];
    char rebuild[2048];
    bool truncated = false;

    char reply[1200];
    memset(reply, 'y', 1100);
    reply[1100] = '\0';
    const size_t n = split_all(reply, 40, arena, sizeof(arena), offsets, 2,
                               &truncated, rebuild, sizeof(rebuild));
    CHECK(n == 2, "cap chunks=2 -> 2 chunks, got %zu", n);
    CHECK(truncated, "over-cap reply MUST set *truncated");
    /* The queued head is still speakable (non-empty). */
    CHECK(strlen(rebuild) > 0, "head must be non-empty");
}

/* ── R7: robodog_transcript_append_chunk ───────────────────────────────── */

static void test_transcript_join(void)
{
    char buf[2048];

    /* "foo." + "Bar." -> "foo. Bar." (one space, no doubling). */
    buf[0] = '\0';
    robodog_transcript_append_chunk(buf, sizeof(buf), 1600, "Hello world.");
    robodog_transcript_append_chunk(buf, sizeof(buf), 1600, "I am the robot!");
    CHECK(strcmp(buf, "Hello world. I am the robot!") == 0,
          "single-space join: got '%s'", buf);

    /* Previous chunk ends with a newline -> no extra space inserted. */
    buf[0] = '\0';
    robodog_transcript_append_chunk(buf, sizeof(buf), 1600, "Line one.\n");
    robodog_transcript_append_chunk(buf, sizeof(buf), 1600, "Line two.");
    CHECK(strcmp(buf, "Line one.\nLine two.") == 0,
          "newline-boundary join: got '%s'", buf);

    /* Next chunk already starts with a space -> no leading space added. */
    buf[0] = '\0';
    robodog_transcript_append_chunk(buf, sizeof(buf), 1600, "Start.");
    robodog_transcript_append_chunk(buf, sizeof(buf), 1600, "  Indented.");
    CHECK(strcmp(buf, "Start.  Indented.") == 0 ||
              strcmp(buf, "Start. Indented.") == 0,
          "next-starts-with-space join: got '%s' (no separator added, text kept)",
          buf);
}

static void test_transcript_cap_keep(void)
{
    /* Append chunks whose total far exceeds the cap: the MOST RECENT chunk
     * must survive INTACT (untruncated), the buffer must be <= cap, and no
     * join rule regression (doubled/lost spaces at the seam). */
    char buf[2048];
    buf[0] = '\0';
    const size_t cap = 256;
    const char *last = "and this is the FINAL sentence that must survive intact";
    for (int i = 0; i < 30; i++) {
        char chunk[64];
        snprintf(chunk, sizeof(chunk), "Sentence number %02d of many.", i);
        robodog_transcript_append_chunk(buf, sizeof(buf), cap, chunk);
    }
    robodog_transcript_append_chunk(buf, sizeof(buf), cap, last);

    CHECK(strlen(buf) <= cap, "buffer %zu chars > cap %zu", strlen(buf), cap);
    CHECK(strstr(buf, last) != NULL,
          "most-recent chunk must survive intact: '%s'", last);
    /* Head dropped, tail kept — never the reverse (the chunk being read
     * must stay on screen, DESIGN.md I2). */
    CHECK(strstr(buf, "Sentence number 00 of many.") == NULL,
          "oldest head must be trimmed away");
}

/* ── R8: robodog_voice_md_strip (markdown -> speech-friendly) ──────────── */

static void prepare_strip(const char *in, const char *want)
{
    char out[256];
    robodog_voice_md_strip(in, out, sizeof(out));
    CHECK(strcmp(out, want) == 0, "md <<%s>> -> got <<%s>> want <<%s>>",
          in, out, want);
}

static void test_md_strip(void)
{
    char out[256];
    size_t n;

    /* The user-reported case: bold-marked headers must not read as
     * "asterisk asterisk". */
    n = robodog_voice_md_strip("**What can you do?**", out, sizeof(out));
    CHECK(strcmp(out, "What can you do?") == 0,
          "bold markers stripped: '%s'", out);
    CHECK(n == strlen(out), "returned len %zu == strlen %zu", n, strlen(out));

    /* Headings, bullets, blockquotes, code spans, links. */
    prepare_strip("## Setup", "Setup");
    prepare_strip("- item one", "item one");
    prepare_strip("> note text", "note text");
    prepare_strip("Go to [docs](http://x/y).", "Go to docs.");
    prepare_strip("`inline` code", "inline code");
    prepare_strip("*em* and _em2_", "em and em2");
    prepare_strip("~~gone~~ still here", "gone still here");

    /* Multi-line markdown: whole-line noise (HR / table separator / fence)
     * is dropped, paragraph breaks survive. */
    prepare_strip("rule above\n---\nrule below", "rule above\nrule below");
    prepare_strip("```\ncode line\n```", "code line");
    prepare_strip("| Name | Value |\n|---|---|\n| A | 1 |",
                  "Name Value\nA 1");

    /* Words that LOOK like markdown but are prose stay intact. */
    prepare_strip("1. first\n2. second", "1. first\n2. second");
    prepare_strip("the temp is -5 degrees", "the temp is -5 degrees");

    /* Whitespace: collapse runs, no leading space after a table pipe or a
     * newline, no trailing space. */
    prepare_strip("Hi!  How are  you?", "Hi! How are you?");
    prepare_strip("a | b", "a b");

    /* Truncation: small buffer still NUL-terminates at cap - 1. */
    n = robodog_voice_md_strip("1234567890**x**", out, 8);
    out[7] = '\0';
    CHECK(strcmp(out, "1234567") == 0 && n == 7,
          "truncated to cap-1: '%s' (n=%zu)", out, n);
}

/* ── R9: voice_lookahead (one-step TTS double-buffer exchange) ────────── */
/* Defined in test_voice_lookahead.c (same translation unit set / cc_test),
 * sharing this harness's s_failures counter. */
extern void test_lookahead_exactly_one(void);
extern void test_lookahead_strict_order(void);
extern void test_lookahead_no_double_claim_playing(void);
extern void test_lookahead_fail_abort(void);
extern void test_lookahead_full_turn(void);
extern void test_lookahead_single_chunk(void);

/* ── main ───────────────────────────────────────────────────────────────── */

int main(void)
{
    test_splitter_lossless_ordered();
    test_splitter_edge_cases();
    test_splitter_truncation_flag();
    test_transcript_join();
    test_transcript_cap_keep();
    test_md_strip();
    test_lookahead_exactly_one();
    test_lookahead_strict_order();
    test_lookahead_no_double_claim_playing();
    test_lookahead_fail_abort();
    test_lookahead_full_turn();
    test_lookahead_single_chunk();

    if (s_failures != 0) {
        fprintf(stderr, "test_voice_split: %d FAILURE(S)\n", s_failures);
        return 1;
    }
    printf("test_voice_split: all checks passed\n");
    return 0;
}
