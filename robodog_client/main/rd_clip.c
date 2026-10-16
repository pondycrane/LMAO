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
