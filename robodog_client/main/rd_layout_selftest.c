/*
 * rd_layout_selftest.c -- host-only self-test for the panel geometry fit
 * arithmetic (NOT part of the ESP-IDF build; defines its own main()).
 *
 *   cc -std=c11 -Wall -Wextra main/rd_layout_selftest.c main/rd_layout.c \
 *      -o /tmp/rd_layout_selftest && /tmp/rd_layout_selftest
 *
 * The invariants proven here are the ones a layout edit can silently break:
 * compact rows never leave the panel or overlap the picture, the row count is
 * exactly what the band holds, and the headline title block never crosses the
 * meta/forecast lines.
 */
#include <stdio.h>

#include "rd_layout.h"

#define CHECK(cond)                                                      \
    do {                                                                 \
        if (!(cond)) {                                                   \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
            return 1;                                                    \
        }                                                                \
        passed++;                                                        \
    } while (0)

/* 9x15B in the renderer's ALL-mode reference height (the font's true bounding
 * box: max_char_height 15 with y_offset -3 -> ascent 12; descent -3).  The
 * renderer measures these at runtime; pinning them here proves the tuned band
 * holds the documented 4 compact rows.  TEXT mode would report 10/-3 and
 * under-space the rows into overlap. */
#define ROW_ASCENT  12
#define ROW_DESCENT (-3)
#define ROW_LINE_H  (ROW_ASCENT - ROW_DESCENT)   /* 15 */

/* helvB18 in ALL mode: max_char_height 29 with y_offset -5 -> ascent 24;
 * descent -5 (line_h 29). */
#define TITLE_ASCENT  24
#define TITLE_DESCENT (-5)
#define TITLE_LINE_H  (TITLE_ASCENT - TITLE_DESCENT)   /* 29 */

/* 10x20 in ALL mode: max_char_height 20 with y_offset -4 -> ascent 16. */
#define META_ASCENT 16

static int test_rows(void)
{
    int passed = 0;
    rd_row_t rows[RD_MAX_EVENT_ROWS];

    size_t n = rd_layout_rows(RD_ROWS_FIRST_BASE, RD_ROWS_BOTTOM_LIMIT,
                              ROW_LINE_H, ROW_DESCENT,
                              RD_MAX_EVENT_ROWS - 1, rows,
                              RD_MAX_EVENT_ROWS - 1);
    /* The tuned geometry holds 4 compact rows (headline + 4 = 5 events). */
    CHECK(n == 4);
    CHECK(rows[0].baseline == 246);
    CHECK(rows[1].baseline == 262);
    CHECK(rows[2].baseline == 278);
    CHECK(rows[3].baseline == 294);
    for (size_t i = 0; i < n; i++) {
        CHECK(rows[i].baseline + ROW_DESCENT <= RD_ROWS_BOTTOM_LIMIT);
        CHECK(rows[i].baseline > RD_PIC_Y + RD_PIC_TILE);  /* below the art */
    }

    /* max_rows is a ceiling, not a promise: asking for 2 yields 2. */
    n = rd_layout_rows(RD_ROWS_FIRST_BASE, RD_ROWS_BOTTOM_LIMIT, ROW_LINE_H,
                       ROW_DESCENT, 2, rows, RD_MAX_EVENT_ROWS);
    CHECK(n == 2);

    /* out_cap truncates the writes but not the reported count. */
    n = rd_layout_rows(RD_ROWS_FIRST_BASE, RD_ROWS_BOTTOM_LIMIT, ROW_LINE_H,
                       ROW_DESCENT, RD_MAX_EVENT_ROWS, rows, 1);
    CHECK(n == 4);

    /* Degenerate inputs fail closed. */
    CHECK(rd_layout_rows(RD_ROWS_FIRST_BASE, RD_ROWS_BOTTOM_LIMIT, 0,
                         ROW_DESCENT, 4, rows, 4) == 0);
    CHECK(rd_layout_rows(RD_ROWS_FIRST_BASE, RD_ROWS_BOTTOM_LIMIT, ROW_LINE_H,
                         1, 4, rows, 4) == 0);
    CHECK(rd_layout_rows(200, 100, ROW_LINE_H, ROW_DESCENT, 4, rows, 4) == 0);
    CHECK(rd_layout_rows(RD_ROWS_FIRST_BASE, RD_ROWS_BOTTOM_LIMIT, ROW_LINE_H,
                         ROW_DESCENT, 4, NULL, 4) == 0);

    /* A taller font yields fewer rows (the band is fixed, the font is not). */
    n = rd_layout_rows(RD_ROWS_FIRST_BASE, RD_ROWS_BOTTOM_LIMIT, 20, -5,
                       RD_MAX_EVENT_ROWS, rows, RD_MAX_EVENT_ROWS);
    CHECK(n == 3);   /* 246/267/288: 20 px lines fit 3, not the 4 of 15 px */

    printf("test_rows: %d checks passed\n", passed);
    return 0;
}

static int test_title_lines(void)
{
    int passed = 0;

    /* bottom_last = meta baseline - meta ascent - gap - title descent. */
    const int bottom_last = RD_HEAD_META_BASE - META_ASCENT - 3 - 5;
    size_t n = rd_layout_title_lines(RD_TITLE_TOP_ANCHOR, bottom_last,
                                     TITLE_LINE_H, TITLE_ASCENT, 8);
    /* helvB18 in a 204 px column: 4 lines (baselines 176/147/118/89), the
     * first line's ink top at 65 >= the 56 anchor. */
    CHECK(n == 4);
    CHECK(bottom_last == 176);
    CHECK(bottom_last - (int)(n - 1) * TITLE_LINE_H - TITLE_ASCENT
          >= RD_TITLE_TOP_ANCHOR);

    /* Never more than the caller's max_lines. */
    CHECK(rd_layout_title_lines(RD_TITLE_TOP_ANCHOR, bottom_last,
                                TITLE_LINE_H, TITLE_ASCENT, 2) == 2);

    /* A band too short for one line fails closed (title dropped, never drawn
     * over the meta). */
    CHECK(rd_layout_title_lines(150, 166, TITLE_LINE_H, TITLE_ASCENT, 8) == 0);
    CHECK(rd_layout_title_lines(0, 10, TITLE_LINE_H, TITLE_ASCENT, 8) == 0);

    /* Degenerate metrics fail closed. */
    CHECK(rd_layout_title_lines(0, 200, 0, TITLE_ASCENT, 8) == 0);
    CHECK(rd_layout_title_lines(0, 200, TITLE_LINE_H, 0, 8) == 0);
    CHECK(rd_layout_title_lines(0, 200, TITLE_LINE_H, TITLE_ASCENT, 0) == 0);

    printf("test_title_lines: %d checks passed\n", passed);
    return 0;
}

static int test_geometry_constants(void)
{
    int passed = 0;

    /* The headline picture fits the panel and leaves a real text column. */
    CHECK(RD_PIC_X + RD_PIC_TILE <= RD_PANEL_W);
    CHECK(RD_PIC_Y + RD_PIC_TILE <= RD_PANEL_H);
    CHECK(RD_TXT_X > RD_PIC_X + RD_PIC_TILE);
    CHECK(RD_TXT_W > 150);   /* wide enough for "HH:MM-HH:MM" at 10x20 */

    /* Rows start below the art and end inside the panel. */
    CHECK(RD_ROWS_FIRST_BASE > RD_PIC_Y + RD_PIC_TILE);
    CHECK(RD_ROWS_BOTTOM_LIMIT < RD_PANEL_H);

    /* Header/status sit above the picture; the text column's blocks stay
     * inside the picture band and in reading order (title, meta, forecast). */
    CHECK(RD_HEADER_BASE < RD_PIC_Y);
    CHECK(RD_STATUS_BASE < RD_PIC_Y);
    CHECK(RD_TITLE_TOP_ANCHOR >= RD_PIC_Y);
    CHECK(RD_HEAD_META_BASE > RD_TITLE_TOP_ANCHOR);
    CHECK(RD_FORECAST_BASE > RD_HEAD_META_BASE);
    CHECK(RD_FORECAST_BASE < RD_PIC_Y + RD_PIC_TILE);

    /* The header rule is a real border in the 4 px gap between the status
     * band's ink bottom (baseline + 10x20 descent 4 = 44) and the tile top. */
    CHECK(RD_HEADER_RULE_Y > RD_STATUS_BASE + 4);
    CHECK(RD_HEADER_RULE_Y < RD_PIC_Y);

    /* The compact-section rule only appears with rows, and must clear both
     * the tile bottom (228) and the first row's glyph top (246 - 12 = 234). */
    CHECK(RD_ROW_RULE_Y > RD_PIC_Y + RD_PIC_TILE);
    CHECK(RD_ROW_RULE_Y < RD_ROWS_FIRST_BASE - ROW_ASCENT);

    /* Every band starts on the same margin and ends on the same right edge. */
    CHECK(RD_LEFT_X == RD_ROWS_LEFT_X);
    CHECK(RD_LEFT_X == RD_PIC_X);
    CHECK(RD_RIGHT_X <= RD_PANEL_W - RD_LEFT_X);
    CHECK(RD_HEADER_SLOT_GAP > 0);

    /* The weather icon box is a real 16x16 slot on BOTH lines that carry it,
     * and it clears the ink of the line above on each:
     *   status  -> below the header's descender ink (header baseline + 4)
     *   forecast-> below the range meta's ink (meta baseline + 4) and inside
     *              the picture band (tile bottom). */
    CHECK(RD_WX_ICON_SZ > 0);
    CHECK(RD_WX_ICON_GAP > 0);
    CHECK(RD_STATUS_BASE - RD_WX_ICON_DY > RD_HEADER_BASE + 4);
    CHECK(RD_STATUS_BASE - RD_WX_ICON_DY + RD_WX_ICON_SZ < RD_HEADER_RULE_Y);
    CHECK(RD_FORECAST_BASE - RD_WX_ICON_DY > RD_HEAD_META_BASE + 4);
    CHECK(RD_FORECAST_BASE - RD_WX_ICON_DY + RD_WX_ICON_SZ
          <= RD_PIC_Y + RD_PIC_TILE);
    CHECK(RD_LEFT_X + RD_WX_ICON_SZ <= RD_RIGHT_X);

    printf("test_geometry_constants: %d checks passed\n", passed);
    return 0;
}

int main(void)
{
    printf("rd_layout self-test\n");
    if (test_rows() != 0 || test_title_lines() != 0 ||
        test_geometry_constants() != 0) {
        printf("RESULT: FAIL\n");
        return 1;
    }
    printf("RESULT: PASS\n");
    return 0;
}
