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
    const size_t n = robodog_voice_split_reply(reply, cap,
                                               ROBODOG_VOICE_SPLIT_MAX_BYTES, 0,
                                               arena, arena_cap, offsets,
                                               max_chunks, truncated);
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
        /* `cap` is a PREFERRED budget: a chunk may run past it to reach a
         * sentence ender, but never past the UI field ceiling. */
        CHECK(l <= ROBODOG_VOICE_SPLIT_MAX_BYTES,
              "chunk %zu is %zu bytes (ceiling=%d)", i, l,
              ROBODOG_VOICE_SPLIT_MAX_BYTES);
    }

    /* SENTENCE ALIGNMENT: every seam here is a real sentence ender, so no
     * chunk may end mid-sentence (the reported "cuts mid-sentence, voice
     * sounds weird" defect). */
    for (size_t i = 0; i + 1 < n; i++) {
        const char *ch = arena + offsets[i];
        const char last = ch[strlen(ch) - 1];
        CHECK(last == '.' || last == '!' || last == '?' || last == ';',
              "chunk %zu ends mid-sentence: '%s'", i, ch);
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
            CHECK(strlen(arena + offsets[i]) <= ROBODOG_VOICE_SPLIT_MAX_BYTES,
                  "chunk %zu exceeds the ceiling (%d)", i,
                  ROBODOG_VOICE_SPLIT_MAX_BYTES);
        }
        /* The '.' sits one byte past the preferred budget, so the splitter
         * must run on to it rather than cut mid-word at 40. */
        CHECK(n == 1 && strlen(arena + offsets[0]) == 41,
              "break just past cap is taken (n=%zu len=%zu)", n,
              n ? strlen(arena + offsets[0]) : 0);
        CHECK(arena[offsets[0] + 40] == '.', "chunk keeps its sentence ender");
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

/* The reported defect: a sentence longer than the preferred budget used to be
 * cut at a space, so the speaker stopped mid-sentence and resumed mid-sentence
 * in the next chunk.  Cuts now land on a sentence end when one fits the
 * ceiling, else on a CLAUSE break (comma/、/：), which is a natural pause —
 * never a bare word cut. */
static void test_splitter_sentence_alignment(void)
{
    char arena[8192];
    size_t offsets[128];
    bool truncated = false;
    const size_t cap = 96;
    const size_t ceiling = cap + 24; /* OVERFLOW_CHARS */

    const char *reply =
        "The living room lamp is currently off, and the blinds are closed, so "
        "the room is fairly dark and you will want more light. "
        "I have turned the lamp on for you. "
        "Your first meeting today is at nine thirty in the morning.";

    const size_t n = robodog_voice_split_reply(reply, cap, ceiling, cap, arena,
                                               sizeof(arena), offsets, 64,
                                               &truncated);
    CHECK(!truncated, "reply fits (n=%zu)", n);
    CHECK(n >= 2, "long sentences still span chunks (n=%zu)", n);
    for (size_t i = 0; i < n; i++) {
        const char *ch = arena + offsets[i];
        const size_t l = strlen(ch);
        CHECK(l <= ceiling, "chunk %zu is %zu bytes (ceiling=%zu)", i, l,
              ceiling);
    }
    /* The 150-byte opening sentence cannot finish inside 96+24, so its first
     * chunk must end at the comma after "closed," — a clause pause, not a
     * word cut in the middle of the clause. */
    const char *c0 = arena + offsets[0];
    CHECK(c0[strlen(c0) - 1] == ',',
          "overlong sentence cut at its clause break: '%s'", c0);
    /* A sentence that DOES fit the ceiling ends at its period. */
    bool saw_period_end = false;
    for (size_t i = 0; i < n; i++) {
        const char *ch = arena + offsets[i];
        const char last = ch[strlen(ch) - 1];
        CHECK(last == '.' || last == ',' || last == ';',
              "chunk %zu ends at a pause: '%s'", i, ch);
        if (last == '.') {
            saw_period_end = true;
        }
    }
    CHECK(saw_period_end, "at least one chunk ends at a full stop");

    /* A decimal point is not a sentence end: "RLCD-4.2" must not split. */
    {
        const char *r =
            "我查過這塊板子（Waveshare ESP32-S3-RLCD-4.2）的硬體清單，上面只"
            "登記了音訊輸入、音訊輸出和螢幕這幾個裝置，並沒有可以控制的燈。";
        const size_t m = robodog_voice_split_reply(r, 96, 120, 96, arena,
                                                   sizeof(arena), offsets, 64,
                                                   &truncated);
        CHECK(m >= 1, "decimal reply splits (m=%zu)", m);
        for (size_t i = 0; i < m; i++) {
            CHECK(strstr(arena + offsets[i], "4.2") != NULL ||
                      strstr(arena + offsets[i], "4.") == NULL,
                  "chunk %zu must not split the decimal 4.2: '%s'", i,
                  arena + offsets[i]);
        }
    }

    /* Chinese: 。 wins when it fits; otherwise the clause comma is used. */
    const char *zh =
        "今天天氣很好。我們可以去公園散步，然後一起吃午餐。"
        "下午可能會下雨，記得帶傘。晚上我們在家看電影。";
    const size_t k = robodog_voice_split_reply(zh, 40, 64, 40, arena,
                                               sizeof(arena), offsets, 64,
                                               &truncated);
    CHECK(!truncated && k >= 2, "CJK reply spans chunks (k=%zu)", k);
    for (size_t i = 0; i < k; i++) {
        const char *ch = arena + offsets[i];
        const size_t l = strlen(ch);
        CHECK(l <= 64, "CJK chunk %zu is %zu bytes (ceiling=64)", i, l);
        CHECK(l >= 3, "CJK chunk %zu is a whole codepoint run", i);
        CHECK(strcmp(ch + l - 3, "。") == 0 ||
                  strcmp(ch + l - 3, "，") == 0 ||
                  strcmp(ch + l - 3, "、") == 0,
              "CJK chunk %zu ends at 。 or ，: '%s'", i, ch);
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

/* ── First-chunk budget: a smaller opening chunk, full-size later ones ─── */
static void test_splitter_first_cap(void)
{
    char arena[4096];
    size_t offsets[64];
    char rebuild[2048];
    bool truncated = false;
    const char *reply =
        "The living room lamp is currently off, and the blinds are closed, so "
        "the room is fairly dark. I have turned the lamp on for you. Your "
        "first meeting today is at nine thirty in the morning.";

    /* first_cap limits ONLY chunk 1; later chunks may use the full cap. */
    const size_t n = split_all(reply, 176, arena, sizeof(arena), offsets, 64,
                               &truncated, rebuild, sizeof(rebuild));
    CHECK(n >= 2, "a 3-sentence reply splits into >= 2 chunks (got %zu)", n);
    CHECK(strlen(arena + offsets[0]) <= 176, "chunk 1 fits the base cap");

    const size_t n2 = robodog_voice_split_reply(
        reply, 176, ROBODOG_VOICE_SPLIT_MAX_BYTES, 96, arena, sizeof(arena),
        offsets, 64, &truncated);
    CHECK(n2 >= n, "the tighter first budget never reduces the chunk count "
          "(%zu vs %zu)", n2, n);
    CHECK(strlen(arena + offsets[0]) <= 96,
          "chunk 1 respects first_cap (len=%zu)", strlen(arena + offsets[0]));
    for (size_t i = 1; i < n2; i++) {
        CHECK(strlen(arena + offsets[i]) <= 176,
              "chunk %zu still uses the base cap (len=%zu)", i + 1,
              strlen(arena + offsets[i]));
    }

    /* first_cap == 0 means "no special first budget". */
    const size_t n3 = robodog_voice_split_reply(
        reply, 176, ROBODOG_VOICE_SPLIT_MAX_BYTES, 0, arena, sizeof(arena),
        offsets, 64, &truncated);
    CHECK(n3 == n, "first_cap=0 matches the plain cap split (%zu vs %zu)", n3,
          n);

    /* A larger first_cap is ignored (it must never widen the base budget). */
    const size_t n4 = robodog_voice_split_reply(
        reply, 176, ROBODOG_VOICE_SPLIT_MAX_BYTES, 400, arena, sizeof(arena),
        offsets, 64, &truncated);
    CHECK(n4 == n, "first_cap larger than cap is ignored (%zu vs %zu)", n4, n);
}

/* ── R9: voice_pipeline (N-slot TTS chunk pipeline) ───────────────────── */
/* Defined in test_voice_pipeline.c (same translation unit set / cc_test),
 * sharing this harness's s_failures counter. */
extern void test_pipe_bounded_inflight(void);
extern void test_pipe_free_slot_reserved(void);
extern void test_pipe_no_steal_of_busy_slots(void);
extern void test_pipe_full_turn(void);
extern void test_pipe_sync_fallback_and_reset(void);
extern void test_pipe_single_chunk(void);

/* ── R11: voice_pcm (chunk-boundary silence trim) ─────────────────────── */
/* Defined in test_voice_pcm.c (same translation unit set / cc_test),
 * sharing this harness's s_failures counter. */
extern void test_pcm_trim_basic(void);
extern void test_pcm_trim_lead_budget(void);
extern void test_pcm_trim_hold_window(void);
extern void test_pcm_trim_degenerate(void);

/* ── R10: UTF-8 display path (CJK) ────────────────────────────────────── */
/* Defined in test_robodog_utf8.c (same translation unit set / cc_test),
 * sharing this harness's s_failures counter. */
extern void test_utf8_decode(void);
extern void test_utf8_sanitize(void);
extern void test_splitter_utf8_cjk(void);
extern void test_transcript_utf8(void);

/* ── main ───────────────────────────────────────────────────────────────── */

int main(void)
{
    test_splitter_lossless_ordered();
    test_splitter_edge_cases();
    test_splitter_sentence_alignment();
    test_splitter_truncation_flag();
    test_transcript_join();
    test_transcript_cap_keep();
    test_md_strip();
    test_utf8_decode();
    test_utf8_sanitize();
    test_splitter_utf8_cjk();
    test_transcript_utf8();
    test_splitter_first_cap();
    test_pipe_bounded_inflight();
    test_pipe_free_slot_reserved();
    test_pipe_no_steal_of_busy_slots();
    test_pipe_full_turn();
    test_pipe_sync_fallback_and_reset();
    test_pipe_single_chunk();
    test_pcm_trim_basic();
    test_pcm_trim_lead_budget();
    test_pcm_trim_hold_window();
    test_pcm_trim_degenerate();

    if (s_failures != 0) {
        fprintf(stderr, "test_voice_split: %d FAILURE(S)\n", s_failures);
        return 1;
    }
    printf("test_voice_split: all checks passed\n");
    return 0;
}
