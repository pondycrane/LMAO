/*
 * rd_layout.h -- the ONE block of panel geometry for the RoboDog calendar
 * render (400x300 landscape logical screen on the ST7305's 300x400 memory).
 *
 * Before this header the geometry was scattered through render_screen as
 * inline literals (img_x/img_y/img_sz, txt_x, the 148 meta baseline, yy=214 /
 * row_stride=20, the y>284 backstop), so a change to one constant silently
 * desynchronised the others.  Everything that positions content lives here;
 * render_screen only draws at these positions.
 *
 * The row/title fit decisions are pure (font metrics are passed in as
 * measured integers) so rd_layout_selftest.c proves the invariants on the host
 * `cc` gate: rows stay inside the panel, never overlap the picture, and the
 * headline title block never crosses into the meta/forecast lines.
 */
#ifndef RD_LAYOUT_H
#define RD_LAYOUT_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ---- Panel (logical, landscape) ---- */
#define RD_PANEL_W 400
#define RD_PANEL_H 300

/* One outer margin for every band (the skill's 4 px): text never starts at
 * the bezel, and every right-aligned value lands on the same edge. */
#define RD_LEFT_X   4
#define RD_RIGHT_X  (RD_PANEL_W - RD_LEFT_X)                /* 396 */

/* ---- Header band ---- */
/* ONE header row: the calendar identity LEFT (kid name, or "family" in family
 * mode) and the live clock RIGHT in a fixed-width slot, so the clock never
 * reflows the label.  Baselines are the skill's documented pair (10x20 header
 * at 22, meta/status at 40); the 10x20 ALL-mode box is asc 16 / desc 4, so
 * the header ink ends at y=26 and the status ink starts at y=24 -- their
 * actual glyphs (digits, lowercase without descenders) stay 4 px apart. */
#define RD_HEADER_BASE 22
#define RD_STATUS_BASE 40
/* Minimum clearance between the LEFT identity label and the RIGHT clock: the
 * label is clipped to the space the clock actually leaves, never assumed to
 * fit (a long kid name must not run into the date). */
#define RD_HEADER_SLOT_GAP 8

/* The header rule is a real 1 px border between the meta band and the content
 * (the skill's "rules are real borders").  It sits at 48: 4 px below the
 * status ink bottom (44) and 4 px above the headline tile (52), so the band
 * reads as one section instead of two floating lines. */
#define RD_HEADER_RULE_Y 48

/* ---- Status band state channel ---- */
/* State is word + shape (never a word alone, never a shape alone): a filled
 * square = live, a hollow square = stale/frozen, a crossed box = failed
 * (offline).  The 7 px square is vertically centred on the 10x20 ink box
 * (baseline-16 .. baseline+4), so its top is baseline-9. */
#define RD_STATUS_SHAPE_SZ  7
#define RD_STATUS_SHAPE_GAP 4
#define RD_STATUS_SHAPE_DY  9

/* ---- Headline block ---- */
/* The served 1-bit tile is RD_PIC_TILE px square and is drawn at its native
 * size (never scaled: u8g2 has no bitmap scaler).  It starts below the header
 * rule and leaves the right column for the headline text.  When the server
 * serves no tile the renderer drops the art slot entirely and gives the
 * headline the FULL width (no bare placeholder frame -- an empty frame reads
 * as a broken panel). */
#define RD_PIC_X    4
#define RD_PIC_Y    52
#define RD_PIC_TILE 176
#define RD_TXT_GAP  12
#define RD_TXT_X    (RD_PIC_X + RD_PIC_TILE + RD_TXT_GAP)   /* 192 */
#define RD_TXT_W    (RD_RIGHT_X - RD_TXT_X)                 /* 204 */

/* Headline text column, in reading order: wrapped title (top), range/qualifier
 * meta, forecast (bottom).  The title is BOTTOM-ANCHORED against the meta line
 * so its last line always clears the meta glyphs; the number of lines that fit
 * is COMPUTED (rd_layout_title_lines), never assumed.  All three stay inside
 * the picture's vertical band (52..228): meta ink 184..204, forecast ink
 * 206..226. */
#define RD_TITLE_TOP_ANCHOR (RD_PIC_Y + 4)   /* 56: below the header rule */
#define RD_HEAD_META_BASE  200
#define RD_FORECAST_BASE   222

/* ---- Compact event rows (below the picture) ---- */
/* Rows use the 9x15B face (claw-parity density: 15 px line height) and are
 * laid out from RD_ROWS_FIRST_BASE down to RD_ROWS_BOTTOM_LIMIT; how many fit
 * is computed from the measured line height (rd_layout_rows).  The section
 * rule above the rows is drawn ONLY when there are compact rows to show (a
 * rule for an absent section is noise): it sits 3 px below the tile bottom
 * (228) and 3 px above the first row's glyph top (234). */
#define RD_ROW_RULE_Y        231
#define RD_ROWS_FIRST_BASE  (RD_PIC_Y + RD_PIC_TILE + 18)    /* 246 */
#define RD_ROWS_BOTTOM_LIMIT (RD_PANEL_H - 2)                /* 298 */
#define RD_ROWS_LEFT_X      RD_LEFT_X
#define RD_ROWS_META_MARGIN 6

/* Hard cap on drawn event rows (headline + compact).  The compact rows are
 * what fit in the band today; this is the defensive ceiling for a future
 * font/geometry change (a 6th row is dropped by design). */
#define RD_MAX_EVENT_ROWS 5

typedef struct {
    int baseline;   /* u8g2 baseline y for this row */
} rd_row_t;

/* Lay out compact rows from first_baseline, spaced line_h + 1 apart, while
 * the glyph bottom (baseline + descent, descent <= 0) stays at/below
 * bottom_limit.  Fills at most out_cap entries and returns the TOTAL row
 * count (<= max_rows), which the caller renders.
 *
 * Degenerate inputs fail closed (0 rows): line_h <= 0, descent > 0, or
 * bottom_limit < first_baseline.
 */
size_t rd_layout_rows(int first_baseline, int bottom_limit, int line_h,
                      int descent, size_t max_rows,
                      rd_row_t *out, size_t out_cap);

/* Number of whole title lines that fit between top_anchor and bottom_last.
 *
 * A line's glyph top is baseline - ascent; the FIRST line must not rise above
 * top_anchor and the LAST line must not drop below bottom_last.  Returns 0
 * when even one line cannot fit (the caller drops the title block for that
 * frame rather than drawing over the meta).  Degenerate metrics fail closed.
 */
size_t rd_layout_title_lines(int top_anchor, int bottom_last, int line_h,
                             int ascent, size_t max_lines);

#ifdef __cplusplus
}
#endif

#endif /* RD_LAYOUT_H */
