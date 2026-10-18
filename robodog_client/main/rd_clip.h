/*
 * rd_clip.h -- pure clip-length primitive for RoboDog row text.
 *
 * The panel's clipped titles (headline helvB24 + compact 10x20 rows) must
 * NEVER overflow their pixel budget, and a clipped title must always carry a
 * visible cut marker.  u8g2 is un-kerned, so a rendered string's width is the
 * exact sum of its per-glyph advances -- a fixed chars-per-px divisor can
 * never bound a proportional font (helvB24 W/M run 23-33 px/char), so the
 * correct width is measured under the ACTIVE font.  This module factors the
 * pure "which prefix fits" decision out of the u8g2 draw path so it is
 * host-testable without the u8g2/ESP-IDF layer (the firmware wraps
 * u8g2_GetStrWidth as the width callback; a host test injects a fake model).
 */
#ifndef RD_CLIP_H
#define RD_CLIP_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Firmware event titles cap at LMA_PB_EVENT_TITLE_MAX (127 bytes); the
 * internal prefix buffer must hold one title + NUL.  robodog.c static-asserts
 * LMA_PB_EVENT_TITLE_MAX <= RD_CLIP_TITLE_MAX so the two stay in lockstep. */
#define RD_CLIP_TITLE_MAX 127u

/* Rendered width in pixels of the NUL-terminated prefix, or 0 for the empty
 * prefix; ctx is opaque.  Must be monotone non-decreasing in prefix length
 * (always true for u8g2: glyph advances are >= 0). */
typedef int (*rd_width_fn)(const char *prefix, void *ctx);

/* ---- multi-line word wrap ----
 *
 * u8g2 has no built-in word wrap (no wrap API in u8g2.h), and the RoboDog
 * headline title is drawn right of a 144 px image in a ~218 px column.  A
 * single clipped helvB24 line ellipsises even a modest title mid-word (seen
 * live: "Peck Open ..."), so the renderer wraps the title into a multi-line
 * helvB18 block sitting above the right-aligned HH:MM-HH:MM meta line.  Like
 * rd_clip_prefix, the wrap decision is pure and measured under the ACTIVE
 * font via the rd_width_fn callback, so it runs identically on the host `cc`
 * gate and in the firmware (which passes u8g2_GetStrWidth). */

/* A wrapped title is at most 8 full lines; a title long enough to need more
 * is truncated with an ellipsis on the final SHOWN line (see rd_wrap_lines).
 * Kept small because the drawn block lives in a ~90 px vertical band above
 * the meta line. */
#define RD_WRAP_MAX_LINES 8u

/* One wrapped line: a byte range into the (NUL-terminated) title string, s.
 * Every line is a contiguous substring of s, so a caller can draw it as-is by
 * copying s[off .. off+len) and NUL-terminating; measured width <= max_px by
 * construction (see rd_wrap_lines). */
typedef struct {
    size_t off;    /* byte offset of this line's first char in s */
    size_t len;    /* byte length of this line (never 0, never splits UTF-8) */
} rd_wrap_line_t;

/* Greedy first-fit word wrap: split s (n = strlen(s)) into lines that fit
 * max_px measured under the ACTIVE font via `width` (same contract as
 * rd_clip_prefix).
 *
 * Algorithm (deliberately simple -- u8g2 is un-kerned so width is exact):
 *   - Tokens are maximal runs of non-space bytes; a run of spaces between two
 *     tokens is treated as ONE indivisible gap, so a break only ever lands on
 *     a space boundary, never in the middle of a word.
 *   - Fill each line greedily: keep appending "gap + token" while the whole
 *     contiguous span still fits max_px.  Lines keep the ORIGINAL spacing of
 *     s (nothing is rewritten into the byte ranges), and every line trims any
 *     leading/trailing space run -- the leading run at line start is skipped,
 *     and a trailing run is only ever flushed as part of the next join (never
 *     left dangling at a line end).
 *   - A single token wider than max_px is hard-broken into the longest
 *     whole-glyph-aligned chunks that each fit max_px (UTF-8-safe: never
 *     splits a multi-byte sequence; boundaries fall on glyph starts).
 *   - max_px <= 0, n == 0, or n > RD_CLIP_TITLE_MAX -> 0 lines (fail closed,
 *     mirroring rd_clip_prefix's negative-budget guard).
 *
 * Fills at most out_cap entries of out[] and returns the TOTAL line count,
 * which may exceed out_cap (the caller draws at most out_cap lines and adds
 * an ellipsis to the last SHOWN line itself).
 *
 * Degenerate budget note: if max_px is smaller than a single glyph in s, that
 * glyph is emitted alone (best-effort -- the wrap never silently drops title
 * bytes).  The production renderer never hits this (218 px column vs ~9-33 px
 * helvB18 glyphs). */
size_t rd_wrap_lines(const char *s, size_t n, int max_px,
                     rd_width_fn width, void *ctx,
                     rd_wrap_line_t *out, size_t out_cap);

/* Largest byte length lo <= n such that:
 *   - lo is a valid UTF-8 boundary (never splits a multi-byte sequence), and
 *   - width(s[0:lo]) + ell_w <= max_px.
 * The caller appends the ellipsis (of measured width ell_w) and draws it.
 * Returns 0 when even a single glyph + ellipsis cannot fit inside max_px (the
 * caller must DROP the clip -- never draw the full untruncated string, which
 * would collide with the right-aligned meta).  Callers run the "whole string
 * fits un-clipped" fast path first; if it is skipped and the whole string
 * happens to fit alongside the ellipsis, lo == n is returned.
 *
 * s must be NUL-terminated and n = strlen(s) <= RD_CLIP_TITLE_MAX.  ell_w is
 * the measured width of the ellipsis glyph/string used; pass 0 only if no
 * cut marker is drawn. */
size_t rd_clip_prefix(const char *s, size_t n, int max_px, int ell_w,
                      rd_width_fn width, void *ctx);

#ifdef __cplusplus
}
#endif

#endif /* RD_CLIP_H */
