/*
 * RoboDog ESP-Claw host unit tests for the pure-C UTF-8 display path.
 *
 * The RLCD used to render every non-ASCII codepoint as '?' (the reply text was
 * ASCII-sanitized), so Traditional Chinese replies showed as a row of question
 * marks.  These tests pin the invariants the panel/TTS path now relies on:
 *   - valid, drawable UTF-8 is preserved byte-for-byte;
 *   - undrawable codepoints become exactly one replacement glyph;
 *   - no buffer boundary (sanitize, chunk split, transcript trim) ever cuts a
 *     multi-byte sequence.
 *
 * Shares the harness + `s_failures` counter of test_voice_split.c (same
 * cc_test translation unit set); the functions below are called from its
 * main().
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#include "robodog_utf8.h"
#include "voice_split.h"
#include "voice_transcript.h"

extern int s_failures;

#define CHECK(cond, fmt, ...)                                                  \
    do {                                                                       \
        if (!(cond)) {                                                         \
            s_failures++;                                                      \
            fprintf(stderr, "FAIL %s:%d: " fmt "\n", __func__, __LINE__,       \
                    ##__VA_ARGS__);                                            \
        }                                                                      \
    } while (0)

/* True when `s` is a well-formed UTF-8 string (decode walks it exactly). */
static bool utf8_valid(const char *s)
{
    const size_t len = strlen(s);
    size_t pos = 0;
    while (pos < len) {
        uint32_t cp = 0;
        const size_t n = robodog_utf8_decode(s + pos, len - pos, &cp);
        if (n == 0) {
            return false;
        }
        pos += n;
    }
    return true;
}

/* ── decoding / boundaries ─────────────────────────────────────────────── */

void test_utf8_decode(void)
{
    uint32_t cp = 0;
    /* ASCII */
    CHECK(robodog_utf8_decode("A", 1, &cp) == 1 && cp == 'A', "ASCII decode");
    /* 你 U+4F60 (E4 BD A0), 。U+3002 (E3 80 82) */
    CHECK(robodog_utf8_decode("\xE4\xBD\xA0", 3, &cp) == 3 && cp == 0x4F60,
          "CJK decode: cp=%04x", cp);
    CHECK(robodog_utf8_decode("\xE3\x80\x82", 3, &cp) == 3 && cp == 0x3002,
          "ideographic full stop: cp=%04x", cp);
    /* 😀 U+1F600 (F0 9F 98 80) */
    CHECK(robodog_utf8_decode("\xF0\x9F\x98\x80", 4, &cp) == 4 &&
              cp == 0x1F600, "4-byte emoji: cp=%04x", cp);
    /* Malformed: lone continuation, truncated sequence, overlong encoding. */
    CHECK(robodog_utf8_decode("\x80", 1, &cp) == 0 &&
              cp == ROBODOG_UTF8_INVALID, "lone continuation rejected");
    CHECK(robodog_utf8_decode("\xE4\xBD", 2, &cp) == 0, "truncated rejected");
    CHECK(robodog_utf8_decode("\xC0\xAF", 2, &cp) == 0, "overlong rejected");

    CHECK(robodog_utf8_strlen("abc") == 3, "strlen ascii");
    CHECK(robodog_utf8_strlen("你好") == 2, "strlen CJK");
    CHECK(robodog_utf8_strlen("a你b") == 3, "strlen mixed");

    /* prev/next boundary never land inside a sequence. */
    const char *s = "a\xE4\xBD\xA0" "b"; /* a 你 b */
    CHECK(robodog_utf8_prev_boundary(s, 2) == 1, "prev inside 你 -> lead");
    CHECK(robodog_utf8_prev_boundary(s, 3) == 1, "prev inside 你 -> lead");
    CHECK(robodog_utf8_next_boundary(s, 2) == 4, "next inside 你 -> after");
    CHECK(robodog_utf8_next_boundary(s, 0) == 0,
          "next at a boundary stays (>= semantics)");

    /* CJK detection picks the reply-language directive when the ASR endpoint
     * does not report a tag. */
    CHECK(!robodog_utf8_has_cjk("hello world"), "ASCII is not CJK");
    CHECK(!robodog_utf8_has_cjk(""), "empty is not CJK");
    CHECK(!robodog_utf8_has_cjk(NULL), "NULL is not CJK");
    CHECK(robodog_utf8_has_cjk("你好世界"), "Chinese is CJK");
    CHECK(robodog_utf8_has_cjk("hello 你好"), "mixed text is CJK");
    CHECK(robodog_utf8_has_cjk("繁體中文"), "Traditional is CJK");
    /* Kana/hangul are NOT Chinese ideographs (the directive must not flip). */
    CHECK(!robodog_utf8_has_cjk("こんにちは"), "kana is not CJK ideograph");
}

/* ── sanitisation ──────────────────────────────────────────────────────── */

void test_utf8_sanitize(void)
{
    char out[256];

    /* The user-reported case: a Traditional Chinese reply must survive
     * unchanged instead of becoming "?????". */
    const char *tc = "你好，世界。今天天氣很好！";
    robodog_utf8_sanitize(out, sizeof(out), tc, '?');
    CHECK(strcmp(out, tc) == 0, "TC reply preserved: got '%s'", out);

    /* English + CJK mix. */
    robodog_utf8_sanitize(out, sizeof(out), "Temp 22°C，你好。", '?');
    CHECK(strcmp(out, "Temp 22°C，你好。") == 0,
          "mixed text preserved: got '%s'", out);

    /* Emoji is undrawable -> exactly ONE '?' per codepoint. */
    robodog_utf8_sanitize(out, sizeof(out), "hi \xF0\x9F\x98\x80 ok", '?');
    CHECK(strcmp(out, "hi ? ok") == 0, "emoji -> one '?': got '%s'", out);

    /* Malformed byte -> '?' and the rest still decodes. */
    robodog_utf8_sanitize(out, sizeof(out), "a\xFF" "b", '?');
    CHECK(strcmp(out, "a?b") == 0, "invalid byte -> '?': got '%s'", out);

    /* Control chars dropped (CRLF's CR included); newline/tab kept. */
    robodog_utf8_sanitize(out, sizeof(out), "a\r\n\tb\x01" "c", '?');
    CHECK(strcmp(out, "a\n\tbc") == 0, "controls dropped: got '%s'", out);

    /* Buffer cap: never splits a sequence, always NUL-terminates. */
    robodog_utf8_sanitize(out, 6, "abc你好", '?');
    CHECK(strcmp(out, "abc") == 0 && utf8_valid(out),
          "cap 6 keeps only 'abc' (你 would need 3 more): got '%s'", out);
    robodog_utf8_sanitize(out, 7, "abc你好", '?');
    CHECK(strcmp(out, "abc你") == 0 && utf8_valid(out),
          "cap 7 fits 'abc你': got '%s'", out);
    robodog_utf8_sanitize(out, 1, "abc", '?');
    CHECK(out[0] == '\0', "cap 1 yields empty string");
}

/* ── chunk splitter: CJK safety ────────────────────────────────────────── */

void test_splitter_utf8_cjk(void)
{
    char arena[4096];
    size_t offsets[128];
    bool truncated = false;

    /* A Chinese reply far longer than the cap: every chunk must be <= cap
     * BYTES and independently valid UTF-8 (no half character at a seam). */
    const char *reply =
        "今天天氣很好。我們可以去公園散步，然後一起吃午餐。"
        "下午可能會下雨，記得帶傘。晚上我們在家看電影。";
    const size_t cap = 40;
    const size_t n = robodog_voice_split_reply(
        reply, cap, ROBODOG_VOICE_SPLIT_MAX_BYTES, 0, arena, sizeof(arena),
        offsets, 64, &truncated);
    CHECK(!truncated, "CJK reply fits 64 chunks (n=%zu)", n);
    CHECK(n > 1, "CJK reply with cap=40 spans multiple chunks (n=%zu)", n);
    size_t rebuilt = 0;
    for (size_t i = 0; i < n; i++) {
        const char *ch = arena + offsets[i];
        CHECK(strlen(ch) <= ROBODOG_VOICE_SPLIT_MAX_BYTES,
              "chunk %zu is %zu bytes (ceiling=%d)", i, strlen(ch),
              ROBODOG_VOICE_SPLIT_MAX_BYTES);
        CHECK(utf8_valid(ch), "chunk %zu is not valid UTF-8: '%s'", i, ch);
        rebuilt += strlen(ch);
    }
    /* Lossless modulo the whitespace seams the splitter intentionally drops
     * (the reply has no spaces, so the byte totals must match exactly). */
    CHECK(rebuilt == strlen(reply),
          "CJK split loses bytes: %zu rebuilt vs %zu source", rebuilt,
          strlen(reply));

    /* A CJK sentence break is preferred over a hard byte cutoff. */
    {
        const char *r = "第一句。第二句。第三句。";
        const size_t k = robodog_voice_split_reply(r, 12, ROBODOG_VOICE_SPLIT_MAX_BYTES, 0, arena,
                                                   sizeof(arena), offsets, 8,
                                                   &truncated);
        CHECK(k == 3, "three 。-terminated sentences -> 3 chunks (got %zu)", k);
        for (size_t i = 0; i < k; i++) {
            const size_t l = strlen(arena + offsets[i]);
            CHECK(l <= 12 && utf8_valid(arena + offsets[i]),
                  "chunk %zu invalid (len=%zu)", i, l);
        }
    }

    /* Cap smaller than one CJK character: the splitter must still advance
     * (emit the codepoint whole) instead of looping. */
    {
        const size_t k = robodog_voice_split_reply("你好", 2, ROBODOG_VOICE_SPLIT_MAX_BYTES, 0, arena,
                                                   sizeof(arena), offsets, 8,
                                                   &truncated);
        CHECK(k == 2 && !truncated, "cap<codepoint emits whole chars (k=%zu)",
              k);
        CHECK(strcmp(arena + offsets[0], "你") == 0 &&
                  strcmp(arena + offsets[1], "好") == 0,
              "cap<codepoint chunks are whole characters");
    }
}

/* ── transcript: CJK-safe join + trim ──────────────────────────────────── */

void test_transcript_utf8(void)
{
    char buf[256];

    /* Join two Chinese chunks: single space, both intact. */
    buf[0] = '\0';
    robodog_transcript_append_chunk(buf, sizeof(buf), 200, "你好。");
    robodog_transcript_append_chunk(buf, sizeof(buf), 200, "世界。");
    CHECK(strcmp(buf, "你好。 世界。") == 0, "CJK join: '%s'", buf);

    /* keep_cap that does NOT land on a codepoint boundary: the retained tail
     * must still be valid UTF-8 and <= keep_cap bytes. */
    buf[0] = '\0';
    for (int i = 0; i < 20; i++) {
        robodog_transcript_append_chunk(buf, sizeof(buf), 31,
                                        "這是一句很長的中文句子。");
    }
    CHECK(strlen(buf) <= 31, "retained %zu bytes > keep_cap 31", strlen(buf));
    CHECK(utf8_valid(buf), "retained tail is valid UTF-8: '%s'", buf);
    CHECK(strstr(buf, "。") != NULL, "retained tail keeps the newest sentence");

    /* buf_size bound mid-character: truncate on a codepoint boundary. */
    buf[0] = '\0';
    robodog_transcript_append_chunk(buf, 8, 200, "你好世界");
    CHECK(strlen(buf) <= 7 && utf8_valid(buf),
          "buf_size bound stays UTF-8 safe: '%s' (%zu B)", buf, strlen(buf));
}
