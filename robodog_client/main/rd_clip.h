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
