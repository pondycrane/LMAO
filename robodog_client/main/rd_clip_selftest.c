/*
 * rd_clip_selftest.c -- host-only self-test for the measured clip/wrap
 * primitives (NOT part of the ESP-IDF build; defines its own main()).
 *
 *   cc -std=c11 -Wall -Wextra main/rd_clip_selftest.c main/rd_clip.c \
 *      -o /tmp/rd_clip_selftest && /tmp/rd_clip_selftest
 *
 * A fake width model (10 px per UTF-8 codepoint) stands in for
 * u8g2_GetStrWidth; the invariants proven are the ones the renderer relies
 * on: a clipped prefix never splits a multi-byte sequence, never exceeds the
 * pixel budget once the ellipsis is added, and a wrapped line is always a
 * contiguous substring that fits.
 */
#include <stdio.h>
#include <string.h>

#include "rd_clip.h"

#define CHECK(cond)                                                      \
    do {                                                                 \
        if (!(cond)) {                                                   \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
            return 1;                                                    \
        }                                                                \
        passed++;                                                        \
    } while (0)

/* 10 px per codepoint: every non-continuation byte starts one glyph. */
static int fake_width(const char *prefix, void *ctx)
{
    (void)ctx;
    int w = 0;
    for (const char *p = prefix; *p; p++) {
        if (((unsigned char)*p & 0xC0u) != 0x80u) {
            w += 10;
        }
    }
    return w;
}

/* Width of the first `len` bytes of s under the fake model. */
static int fake_width_len(const char *s, size_t len)
{
    char tmp[RD_CLIP_TITLE_MAX + 1];
    if (len > RD_CLIP_TITLE_MAX) {
        return 1 << 20;
    }
    memcpy(tmp, s, len);
    tmp[len] = '\0';
    return fake_width(tmp, NULL);
}

static int is_boundary(const char *s, size_t i)
{
    return i == 0 || ((unsigned char)s[i] & 0xC0u) != 0x80u;
}

static int test_boundaries(void)
{
    int passed = 0;
    const char *s = "a\xE4\xB8\xADz";   /* 'a', U+4E2D (3 bytes), 'z' */

    CHECK(rd_prev_utf8_boundary(s, 4) == 4);  /* 'z' starts a glyph */
    CHECK(rd_prev_utf8_boundary(s, 3) == 1);  /* inside U+4E2D -> its lead */
    CHECK(rd_prev_utf8_boundary(s, 2) == 1);
    CHECK(rd_prev_utf8_boundary(s, 1) == 1);  /* already a boundary */
    CHECK(rd_prev_utf8_boundary(s, 0) == 0);
    CHECK(rd_next_utf8_boundary(s, 1, 5) == 4);
    CHECK(rd_next_utf8_boundary(s, 0, 5) == 1);
    CHECK(rd_next_utf8_boundary(s, 4, 5) == 5);

    printf("test_boundaries: %d checks passed\n", passed);
    return 0;
}

static int test_clip_prefix(void)
{
    int passed = 0;
    const char *s = "Swim class \xE4\xB8\xAD\xE6\x96\x87 now";   /* CJK inside */
    const size_t n = strlen(s);

    /* Budget for ~2 glyphs + ellipsis (3 glyphs = 30 px). */
    size_t lo = rd_clip_prefix(s, n, 35, 10, fake_width, NULL);
    CHECK(lo > 0 && lo < n);
    CHECK(is_boundary(s, lo));
    CHECK(fake_width_len(s, lo) + 10 <= 35);

    /* Whole string fits alongside the ellipsis -> lo == n. */
    lo = rd_clip_prefix(s, n, 10000, 10, fake_width, NULL);
    CHECK(lo == n);

    /* A budget smaller than one glyph + ellipsis fails closed (drop). */
    CHECK(rd_clip_prefix(s, n, 15, 10, fake_width, NULL) == 0);
    CHECK(rd_clip_prefix(s, n, 0, 10, fake_width, NULL) == 0);
    CHECK(rd_clip_prefix(s, n, 35, -1, fake_width, NULL) == 0);
    CHECK(rd_clip_prefix(s, 0, 35, 10, fake_width, NULL) == 0);
    CHECK(rd_clip_prefix(s, RD_CLIP_TITLE_MAX + 1, 35, 10, fake_width,
                         NULL) == 0);

    /* Every returned prefix is a UTF-8 boundary across the whole budget
     * range -- the property the old three-copy back-off could break. */
    for (int budget = 20; budget <= 200; budget += 5) {
        lo = rd_clip_prefix(s, n, budget, 10, fake_width, NULL);
        if (lo > 0) {
            CHECK(is_boundary(s, lo));
            CHECK(fake_width_len(s, lo) + 10 <= budget);
        }
    }

    printf("test_clip_prefix: %d checks passed\n", passed);
    return 0;
}

static int test_wrap_lines(void)
{
    int passed = 0;
    rd_wrap_line_t lines[RD_WRAP_MAX_LINES];
    const char *s = "Mom pickup at school gate";
    const size_t n = strlen(s);

    size_t nl = rd_wrap_lines(s, n, 100, fake_width, NULL, lines,
                              RD_WRAP_MAX_LINES);
    CHECK(nl == 3);   /* "Mom pickup" / "at school" / "gate" */
    for (size_t i = 0; i < nl && i < RD_WRAP_MAX_LINES; i++) {
        char tmp[RD_CLIP_TITLE_MAX + 1];
        memcpy(tmp, s + lines[i].off, lines[i].len);
        tmp[lines[i].len] = '\0';
        CHECK(lines[i].len > 0);
        CHECK(fake_width(tmp, NULL) <= 100);
    }

    /* A single over-wide token is hard-broken on UTF-8 boundaries. */
    {
        const char *t = "\xE4\xB8\xAD\xE6\x96\x87\xE4\xB8\xAD\xE6\x96\x87";
        size_t tn = strlen(t);
        size_t cnt = rd_wrap_lines(t, tn, 10, fake_width, NULL, lines,
                                   RD_WRAP_MAX_LINES);
        CHECK(cnt >= 2);
        for (size_t i = 0; i < cnt && i < RD_WRAP_MAX_LINES; i++) {
            CHECK(is_boundary(t, lines[i].off));
            CHECK(is_boundary(t, lines[i].off + lines[i].len));
            CHECK(lines[i].len == 3);   /* one glyph per line at 10 px */
        }
    }

    /* out_cap truncates the writes but not the reported line count. */
    CHECK(rd_wrap_lines(s, n, 100, fake_width, NULL, lines, 1) == 3);

    /* Degenerate inputs fail closed. */
    CHECK(rd_wrap_lines(s, n, 0, fake_width, NULL, lines, 4) == 0);
    CHECK(rd_wrap_lines(s, 0, 100, fake_width, NULL, lines, 4) == 0);
    CHECK(rd_wrap_lines(s, RD_CLIP_TITLE_MAX + 1, 100, fake_width, NULL,
                        lines, 4) == 0);
    CHECK(rd_wrap_lines(s, n, 100, fake_width, NULL, NULL, 4) == 0);

    printf("test_wrap_lines: %d checks passed\n", passed);
    return 0;
}

int main(void)
{
    printf("rd_clip self-test\n");
    if (test_boundaries() != 0 || test_clip_prefix() != 0 ||
        test_wrap_lines() != 0) {
        printf("RESULT: FAIL\n");
        return 1;
    }
    printf("RESULT: PASS\n");
    return 0;
}
