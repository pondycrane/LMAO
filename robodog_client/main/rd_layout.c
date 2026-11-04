/*
 * rd_layout.c -- pure row/title fit arithmetic (see rd_layout.h).
 *
 * Dependency-free (stddef only) so it builds and self-tests on a bare host
 * `cc -std=c11 -Wall -Wextra` as well as inside the ESP-IDF main component.
 * The firmware passes the font metrics it measured from u8g2 at runtime, so
 * the fit decisions track the ACTIVE font rather than assumed constants.
 */
#include "rd_layout.h"

size_t rd_layout_rows(int first_baseline, int bottom_limit, int line_h,
                      int descent, size_t max_rows,
                      rd_row_t *out, size_t out_cap)
{
    /* Degenerate geometry: a non-positive line height, a positive descent
     * (u8g2 descents are <= 0, so this is a caller bug), an inverted band, or
     * a NULL sink with a non-zero capacity -- fail closed like every other
     * out-of-contract input in this firmware. */
    if (line_h <= 0 || descent > 0 || bottom_limit < first_baseline ||
        (out == NULL && out_cap > 0)) {
        return 0;
    }

    size_t n = 0;
    int base = first_baseline;
    while (n < max_rows && base + descent <= bottom_limit) {
        if (n < out_cap) {
            out[n].baseline = base;
        }
        n++;
        base += line_h + 1;   /* one clear pixel between rows */
    }
    return n;
}

size_t rd_layout_title_lines(int top_anchor, int bottom_last, int line_h,
                             int ascent, size_t max_lines)
{
    if (line_h <= 0 || ascent <= 0 || bottom_last < top_anchor ||
        max_lines == 0) {
        return 0;
    }
    /* n lines occupy baselines bottom_last, bottom_last-line_h, ...; the
     * first (highest) line's glyph top is bottom_last-(n-1)*line_h-ascent. */
    int band = bottom_last - top_anchor - ascent;
    if (band < 0) {
        return 0;
    }
    size_t n = (size_t)(band / line_h) + 1;
    return n > max_lines ? max_lines : n;
}
