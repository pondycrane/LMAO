/*
 * rd_clip.c -- pure clip-length primitive (see rd_clip.h).
 *
 * Deliberately dependency-free (stdlib only) so this builds and self-tests on
 * a bare host `cc -std=c11 -Wall -Wextra` as well as inside the ESP-IDF main
 * component.  The firmware passes a u8g2_GetStrWidth wrapper as the width
 * callback; because u8g2 is un-kerned that width is exact, so the returned
 * clipped prefix is guaranteed to fit max_px under the ACTIVE font.
 */
#include "rd_clip.h"

#include <stdint.h>
#include <string.h>

size_t rd_clip_prefix(const char *s, size_t n, int max_px, int ell_w,
                      rd_width_fn width, void *ctx)
{
    if (max_px <= 0 || ell_w < 0 || n == 0 || n > RD_CLIP_TITLE_MAX) {
        /* Non-positive budget, a bogus negative ellipsis width (would let a
         * clip under-reserve and overrun the budget once the real ellipsis is
         * appended), nothing to clip, or an out-of-contract title: fail
         * closed.  (A negative max_px is the classic negative-int ->
         * huge-size_t cast footgun; rejecting it here means a full
         * untruncated title can never be drawn into a zero/negative box.) */
        return 0;
    }

    /* Width is monotone in the prefix length (u8g2 glyph advances are >= 0),
     * so walk back from the full title one WHOLE glyph at a time until the
     * budget holds: the first fit found is the longest one that fits.  A
     * byte-index binary search cannot be used directly -- a candidate byte can
     * land inside a multi-byte sequence, and the nearest valid UTF-8 boundary
     * is not monotone in the byte index, so a binary search can skip the real
     * fitting boundary or, worse, loop forever without narrowing.  n is
     * bounded by RD_CLIP_TITLE_MAX (127) and clipping runs on at most 3 rows
     * per 30 s frame, so the linear walk is trivially cheap. */
    size_t c = n;
    while (c > 0) {
        char tmp[RD_CLIP_TITLE_MAX + 1];
        memcpy(tmp, s, c);
        tmp[c] = '\0';
        if (width(tmp, ctx) + ell_w <= max_px) {
            return c;                 /* longest prefix (+ ellipsis) that fits */
        }
        c--;                          /* drop one whole glyph... */
        while (c > 0 && ((uint8_t)s[c] & 0xC0) == 0x80) {
            c--;                      /* ...without splitting a UTF-8 sequence */
        }
    }
    /* Even a single glyph + ellipsis cannot fit: fail closed (0 == drop). */
    return 0;
}

/* Append one wrapped line [off, len) to out[] (recording only the first
 * out_cap lines) and return the updated total line count.  The caller owns
 * the off/len reset; the single flush point guarantees each line is emitted
 * once and out[] is never written past out_cap. */
static size_t rd_wrap_emit(rd_wrap_line_t *out, size_t out_cap,
                           size_t nlines, size_t off, size_t len)
{
    if (nlines < out_cap) {
        out[nlines].off = off;
        out[nlines].len = len;
    }
    return nlines + 1;
}

size_t rd_wrap_lines(const char *s, size_t n, int max_px,
                     rd_width_fn width, void *ctx,
                     rd_wrap_line_t *out, size_t out_cap)
{
    /* Negative/zero budget (classic int -> huge-size_t cast footgun), nothing
     * to wrap, an out-of-contract title, or a NULL out with out_cap > 0:
     * fail closed, exactly like rd_clip_prefix.  n is thus <=
     * RD_CLIP_TITLE_MAX below, so any emitted line (a contiguous substring of
     * s) fits our 128-byte scratch buffer. */
    if (max_px <= 0 || n == 0 || n > RD_CLIP_TITLE_MAX ||
        (out == NULL && out_cap > 0)) {
        return 0;
    }

    char tmp[RD_CLIP_TITLE_MAX + 1];
    size_t nlines = 0;        /* total lines found (may exceed out_cap) */
    size_t off = 0, len = 0;  /* current line's byte range in s (trimmed) */
    size_t i = 0;             /* scan position */

    while (i < n) {
        /* A line never starts with spaces: skip the leading run (also trims
         * trailing spaces once nothing follows them, and treats an interior
         * separator run as one logical gap whose bytes stay in the line). */
        while (i < n && s[i] == ' ') {
            i++;
        }
        if (i >= n) {
            break;            /* trailing space run: nothing left to wrap */
        }

        /* One whole token = a maximal run of non-space bytes. */
        const size_t tok = i;
        while (i < n && s[i] != ' ') {
            i++;
        }
        const size_t toklen = i - tok;

        /* Try to join this token onto the open line.  The candidate is the
         * CONTIGUOUS span [off, tok+toklen) -- it includes the original
         * separator run -- so the measured width is exactly what would be
         * drawn, and the emitted line stays a plain substring of s. */
        if (len > 0) {
            const size_t span = (tok + toklen) - off;
            if (span > RD_CLIP_TITLE_MAX) {
                /* Unreachable (n <= RD_CLIP_TITLE_MAX here); never open a
                 * line wider than the 128-byte scratch buffer -- fail closed
                 * like every other out-of-contract input. */
                return 0;
            }
            memcpy(tmp, s + off, span);
            tmp[span] = '\0';
            if (width(tmp, ctx) <= max_px) {
                len = span;   /* off is unchanged: line now spans to the token */
                continue;
            }
            /* Overflows the line: close it and re-seat the token on a fresh
             * line (the token's byte range is deliberately NOT advanced over
             * -- the fresh-line handling below picks it back up). */
            nlines = rd_wrap_emit(out, out_cap, nlines, off, len);
            off = 0;
            len = 0;
        }

        /* Fresh line: does the whole token fit on its own? */
        if (toklen <= RD_CLIP_TITLE_MAX) {
            memcpy(tmp, s + tok, toklen);
            tmp[toklen] = '\0';
        }
        if (width(tmp, ctx) <= max_px) {
            off = tok;
            len = toklen;
            continue;
        }

        /* A single token is wider than the budget: hard-break it into the
         * longest whole-glyph-aligned chunks that each fit max_px.  One byte
         * at a time would split UTF-8; instead mirror rd_clip_prefix's walk:
         * veto a candidate byte that is a continuation byte and back off one
         * WHOLE glyph (the nearest valid UTF-8 boundary is not monotone in the
         * byte index, so a binary search cannot be used safely here either). */
        size_t c = 0;
        while (c < toklen) {
            size_t cc = toklen - c;       /* longest candidate chunk */
            while (cc > 0) {
                memcpy(tmp, s + tok + c, cc);
                tmp[cc] = '\0';
                if (width(tmp, ctx) <= max_px) {
                    break;
                }
                cc--;                     /* drop one whole glyph... */
                while (cc > 0 &&
                       ((uint8_t)s[tok + c + cc] & 0xC0) == 0x80) {
                    cc--;                 /* ...without splitting UTF-8 */
                }
            }
            if (cc == 0) {
                /* Degenerate budget: even one glyph is over width.  Best
                 * effort -- emit that single glyph alone so no bytes are
                 * dropped (unreachable at the production 218 px budget). */
                cc = 1;
                while (c + cc < toklen &&
                       ((uint8_t)s[tok + c + cc] & 0xC0) == 0x80) {
                    cc++;
                }
            }
            nlines = rd_wrap_emit(out, out_cap, nlines, tok + c, cc);
            c += cc;
        }
        /* Token fully consumed; i already points just past it. */
    }

    /* Flush the final open line (kept empty when s was all spaces / empty). */
    if (len > 0) {
        nlines = rd_wrap_emit(out, out_cap, nlines, off, len);
    }
    return nlines;
}
