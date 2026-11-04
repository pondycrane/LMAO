/*
 * rd_render.c -- ST7305 panel rendering (see rd_render.h).
 *
 * Layout constants come from rd_layout.h; font metrics are MEASURED from
 * u8g2 at runtime (never assumed), and the number of title lines / compact
 * rows that fit is computed from those measurements (rd_layout_rows /
 * rd_layout_title_lines), so a font change cannot silently push text off the
 * panel or into the next block.
 */
#include "rd_render.h"

#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"

#include "config.h"
#include "lma_proto.h"
#include "rd_clip.h"
#include "rd_datetime.h"
#include "rd_layout.h"
#include "rd_weather.h"
#include "u8g2_st7305.h"

static const char *TAG = "rd_render";

/* rd_clip's internal prefix buffer must hold any server-served title. */
_Static_assert(LMA_PB_EVENT_TITLE_MAX <= RD_CLIP_TITLE_MAX,
               "rd_clip prefix buffer must cover the event title cap");

/* Faces: header/status/meta 10x20; headline title helvB18; compact rows
 * 9x15B (claw-parity 15 px density on the same panel). */
#define RD_FONT_UI    u8g2_font_10x20_tf
#define RD_FONT_TITLE u8g2_font_helvB18_tf
#define RD_FONT_ROW   u8g2_font_9x15B_tf

/* Clearance between the headline title block and the meta line above it. */
#define RD_TITLE_META_GAP 3

static u8g2_st7305_t s_display;

/* Set a font AND switch its reference height to the ALL mode, so
 * GetAscent/GetDescent report the font's true bounding box (tallest glyph +
 * descender) rather than the TEXT-mode nominal line.  TEXT mode reports
 * 9x15B as 10/-3 while real glyphs reach 12 above and 3 below the baseline;
 * spacing rows on the nominal box would overlap them.  All layout arithmetic
 * (rd_layout) is fed these measured values. */
static void set_font(u8g2_t *u8g2, const uint8_t *font)
{
    u8g2_SetFont(u8g2, font);
    u8g2_SetFontRefHeightAll(u8g2);
}

/* ---- Display clock ----------------------------------------------------- */
static uint64_t s_epoch_base_ms = 0;    /* 0 = no clock yet (pre-first-sync) */
static uint64_t s_epoch_base_tick = 0;  /* esp_timer ms when base was set */

void rd_render_set_clock(uint64_t utc_ms)
{
    s_epoch_base_ms = utc_ms;
    s_epoch_base_tick = (uint64_t)(esp_timer_get_time() / 1000);
}

uint64_t rd_render_now_ms(void)
{
    if (s_epoch_base_ms == 0) {
        return 0;
    }
    return s_epoch_base_ms +
           ((uint64_t)(esp_timer_get_time() / 1000) - s_epoch_base_tick);
}

/* ---- Shot capture ------------------------------------------------------ */
/* Always-on: stream the exact u8g2 full-frame buffer (== the bytes pushed to
 * the ST7305) over the USB-JTAG console so a host tool can reconstruct the
 * image the firmware drew, pixel for pixel.  Protocol: RD_SHOT_BEGIN
 * /<bufsize,w,h>/lowercase-hex/RD_SHOT_END. */
static void rd_shot_dump(u8g2_t *u8g2)
{
    const uint8_t *buf = u8g2_GetBufferPtr(u8g2);
    const size_t n = u8g2_GetBufferSize(u8g2);
    printf("\nRD_SHOT_BEGIN bufsize=%u w=%d h=%d\n", (unsigned)n,
           u8g2_GetDisplayWidth(u8g2), u8g2_GetDisplayHeight(u8g2));
    for (size_t i = 0; i < n; i++) {
        printf("%02x", buf[i]);
        if ((i & 0x1f) == 0x1f) {
            printf("\n");
        }
    }
    printf("\nRD_SHOT_END\n");
    fflush(stdout);
}

/* ---- Draw helpers ------------------------------------------------------ */
static void draw_centered(u8g2_t *u8g2, int baseline_y, const char *s)
{
    int w = u8g2_GetStrWidth(u8g2, s);
    int x = (u8g2_GetDisplayWidth(u8g2) - w) / 2;
    if (x < 0) {
        x = 0;
    }
    u8g2_DrawStr(u8g2, x, baseline_y, s);
}

static void draw_right(u8g2_t *u8g2, int right_x, int baseline_y, const char *s)
{
    u8g2_DrawStr(u8g2, right_x - u8g2_GetStrWidth(u8g2, s), baseline_y, s);
}

static int rd_u8g2_width(const char *prefix, void *ctx)
{
    return u8g2_GetStrWidth((u8g2_t *)ctx, prefix);
}

/* The ellipsis the ACTIVE font can actually draw: real "…" (U+2026) when the
 * font carries it, plain "..." otherwise (the _tf subsets lack it and u8g2
 * would render a fallback blob). */
static const char *ellipsis_for(u8g2_t *u8g2)
{
    return u8g2_IsGlyph(u8g2, 0x2026) ? "\xE2\x80\xA6" : "...";
}

/* Clip a string to max_px by MEASURED width under the active font (u8g2 is
 * un-kerned, so u8g2_GetStrWidth is exact).  A zero/negative budget drops the
 * string loudly instead of drawing it into the meta. */
static void draw_clipped(u8g2_t *u8g2, int x, int base, const char *s,
                         int max_px)
{
    if (max_px <= 0) {
        ESP_LOGW(TAG, "clip budget %dpx <= 0; dropping '%s'", max_px, s);
        return;
    }
    const size_t n = strlen(s);
    if ((int)u8g2_GetStrWidth(u8g2, s) <= max_px) {
        u8g2_DrawStr(u8g2, x, base, s);   /* whole string fits: no marker */
        return;
    }
    const char *ell = ellipsis_for(u8g2);
    const int ell_w = u8g2_GetStrWidth(u8g2, ell);
    const size_t lo = rd_clip_prefix(s, n, max_px, ell_w, rd_u8g2_width,
                                     u8g2);
    if (lo == 0) {
        ESP_LOGW(TAG, "no prefix of '%s' + ellipsis fits in %dpx; dropping",
                 s, max_px);
        return;
    }
    char tmp[RD_CLIP_TITLE_MAX + 4];   /* line + "…" (3B) + NUL always fit */
    memcpy(tmp, s, lo);
    tmp[lo] = '\0';
    strncat(tmp, ell, sizeof(tmp) - strlen(tmp) - 1);
    u8g2_DrawStr(u8g2, x, base, tmp);
}

/* Final SHOWN line of a vertically-truncated wrapped headline title: always
 * carries the cut marker, even when the line itself fits horizontally,
 * because it signals the hidden tail. */
static void draw_wrapped_final(u8g2_t *u8g2, int x, int base,
                               const char *s, size_t len, int max_px)
{
    if (max_px <= 0) {
        ESP_LOGW(TAG, "wrapped final-line budget %dpx <= 0; dropping '%s'",
                 max_px, s);
        return;
    }
    const char *ell = ellipsis_for(u8g2);
    const int ell_w = u8g2_GetStrWidth(u8g2, ell);
    const size_t lo = rd_clip_prefix(s, len, max_px, ell_w, rd_u8g2_width,
                                     u8g2);
    char tmp[RD_CLIP_TITLE_MAX + 4];
    if (lo == 0) {
        if (ell_w > max_px) {
            ESP_LOGW(TAG, "wrapped final line: ellipsis %dpx > %dpx budget; "
                          "dropping '%s'", ell_w, max_px, s);
            return;
        }
        memcpy(tmp, ell, strlen(ell) + 1);
    } else {
        memcpy(tmp, s, lo);
        tmp[lo] = '\0';
        strncat(tmp, ell, sizeof(tmp) - strlen(tmp) - 1);
    }
    u8g2_DrawStr(u8g2, x, base, tmp);
}

/* ---- Render ------------------------------------------------------------ */
static void draw_header(u8g2_t *u8g2, const rd_config_t *cfg, uint64_t now)
{
    if (now == 0) {
        return;   /* pre-first-sync: no clock to show */
    }
    char line[32];
    set_font(u8g2, RD_FONT_UI);
    rd_utc_ms_to_datetime(now, RD_TZ_OFFSET_MINUTES, line, sizeof(line));
    if (cfg->kid_name[0] != '\0') {
        u8g2_DrawStr(u8g2, 6, RD_HEADER_BASE, cfg->kid_name);
        draw_right(u8g2, RD_RIGHT_X, RD_HEADER_BASE, line);
    } else {
        draw_centered(u8g2, RD_HEADER_BASE, line);
    }
}

/* Status/weather band: current conditions LEFT ("now 44°F clear"), the
 * stale/no-wifi hint RIGHT.  Nothing is drawn when the server sent no
 * Weather -- never a placeholder reading. */
static void draw_status(u8g2_t *u8g2, rd_state_t state,
                        const lma_weather_t *current)
{
    set_font(u8g2, RD_FONT_UI);
    const char *hint = (state == RD_STATE_STALE) ? "stale" :
                       (state == RD_STATE_OFFLINE) ? "no wifi" : NULL;
    int right = RD_RIGHT_X;
    if (hint != NULL) {
        draw_right(u8g2, RD_RIGHT_X, RD_STATUS_BASE, hint);
        right -= u8g2_GetStrWidth(u8g2, hint) + 8;
    }
    char wx[24];
    if (current->present && rd_format_weather(current, wx, sizeof(wx))) {
        char band[32];
        snprintf(band, sizeof(band), "now %s", wx);
        draw_clipped(u8g2, 6, RD_STATUS_BASE, band, right - 6);
    }
}

/* Headline title: word-wrapped helvB18 block in the text column, bottom-
 * anchored above the forecast line.  The ellipsis lands on the final SHOWN
 * line only when the wrapped block overflows the band. */
static void draw_headline_title(u8g2_t *u8g2, const char *title)
{
    set_font(u8g2, RD_FONT_TITLE);
    const int t_asc = u8g2_GetAscent(u8g2);
    const int t_desc = u8g2_GetDescent(u8g2);   /* <= 0 */
    const int t_line_h = t_asc - t_desc;

    set_font(u8g2, RD_FONT_UI);
    const int meta_asc = u8g2_GetAscent(u8g2);
    if (t_line_h <= 0 || meta_asc <= 0) {
        static bool warned = false;
        if (!warned) {
            warned = true;
            ESP_LOGW(TAG, "degenerate font metrics: title line_h=%d, meta "
                          "ascent=%d; title block dropped",
                     t_line_h, meta_asc);
        }
        return;
    }
    set_font(u8g2, RD_FONT_TITLE);

    const int max_px = RD_TXT_W;
    rd_wrap_line_t wl[RD_WRAP_MAX_LINES];
    const size_t nl = rd_wrap_lines(title, strlen(title), max_px,
                                    rd_u8g2_width, u8g2, wl,
                                    RD_WRAP_MAX_LINES);

    /* Last baseline clears the meta glyph top; the first line stays at/below
     * RD_TITLE_TOP_ANCHOR.  n_fit is computed, never assumed. */
    const int bottom_last =
        RD_HEAD_META_BASE - meta_asc - RD_TITLE_META_GAP - (-t_desc);
    size_t n_fit = rd_layout_title_lines(RD_TITLE_TOP_ANCHOR, bottom_last,
                                         t_line_h, t_asc, RD_WRAP_MAX_LINES);
    if (n_fit > nl) {
        n_fit = nl;
    }
    const bool truncated = (nl > n_fit);

    for (size_t k = 0; k < n_fit; k++) {
        const int base = bottom_last - (int)(n_fit - 1 - k) * t_line_h;
        char tmp[LMA_PB_EVENT_TITLE_MAX + 1];
        const size_t lo = wl[k].off, ln = wl[k].len;
        memcpy(tmp, title + lo, ln);
        tmp[ln] = '\0';
        if (k == n_fit - 1 && truncated) {
            draw_wrapped_final(u8g2, RD_TXT_X, base, tmp, ln, max_px);
        } else {
            u8g2_DrawStr(u8g2, RD_TXT_X, base, tmp);
        }
    }
    if (truncated) {
        ESP_LOGW(TAG, "headline '%s' wrapped to %u lines, showing %u",
                 title, (unsigned)nl, (unsigned)n_fit);
    }
}

static void draw_headline(u8g2_t *u8g2, const rd_cache_t *cache,
                          const lma_cal_event_t *hl,
                          int64_t day_now, int yr_now)
{
    /* Picture: the served 176x176 tile, or a tidy empty slot when the server
     * served none (or the cached tile belongs to another event). */
    const bool tile_ok = cache->pic_data_len == LMA_PB_PIC_TILE_BYTES &&
                         strcmp(cache->pic_uid, hl->uid) == 0;
    if (tile_ok) {
        /* u8g2_DrawBitmap (MSB-first, bit 7 = leftmost), NOT DrawXBM
         * (LSB-first): the served tile is MSB-first row-major, "1 = ink". */
        u8g2_DrawBitmap(u8g2, RD_PIC_X, RD_PIC_Y, RD_PIC_TILE >> 3,
                        RD_PIC_TILE, cache->pic_data);
    } else {
        ESP_LOGW(TAG, "headline uid=%s has no tile (len=%u, expected %u); "
                      "drawing placeholder",
                 hl->uid, (unsigned)cache->pic_data_len,
                 (unsigned)LMA_PB_PIC_TILE_BYTES);
        u8g2_DrawFrame(u8g2, RD_PIC_X, RD_PIC_Y, RD_PIC_TILE, RD_PIC_TILE);
    }

    /* Range + day qualifier, right-aligned in the text column. */
    set_font(u8g2, RD_FONT_UI);
    char meta[32];
    rd_format_event_meta(hl, RD_TZ_OFFSET_MINUTES, day_now, yr_now,
                         meta, sizeof(meta));
    draw_right(u8g2, RD_RIGHT_X, RD_HEAD_META_BASE, meta);

    draw_headline_title(u8g2, hl->title);

    /* Forecast at the headline event's start (absent -> nothing drawn). */
    set_font(u8g2, RD_FONT_UI);
    char wx[24];
    if (hl->weather.present && rd_format_weather(&hl->weather, wx, sizeof(wx))) {
        draw_clipped(u8g2, RD_TXT_X, RD_FORECAST_BASE, wx, RD_TXT_W);
    }
}

void rd_render_screen(rd_state_t state, const rd_config_t *cfg,
                      const lma_weather_t *current, const rd_cache_t *cache)
{
    u8g2_t *u8g2 = u8g2_st7305_get_u8g2(&s_display);
    const int W = u8g2_GetDisplayWidth(u8g2);
    const int H = u8g2_GetDisplayHeight(u8g2);

    /* Invert: clear fills black; paint the whole panel white, then draw all
     * content in black -> white paper, black ink. */
    u8g2_ClearBuffer(u8g2);
    u8g2_SetDrawColor(u8g2, 1);
    u8g2_DrawBox(u8g2, 0, 0, W, H);
    u8g2_SetDrawColor(u8g2, 0);

    const uint64_t now = rd_render_now_ms();
    draw_header(u8g2, cfg, now);
    draw_status(u8g2, state, current);

    if (cache->count == 0) {
        const char *msg = (state == RD_STATE_OFFLINE) ? "no wifi  :  offline"
                                                      : "no calendar events";
        set_font(u8g2, RD_FONT_UI);
        draw_centered(u8g2, (H / 2) + 7, msg);
        u8g2_SendBuffer(u8g2);
        rd_shot_dump(u8g2);
        return;
    }

    /* Qualifier anchor: local civil day + year of the display clock.  0/0
     * before the first sync -> rows render range-only. */
    int64_t day_now = 0;
    int yr_now = 0;
    if (now != 0) {
        int mo_tmp, dy_tmp, hh_tmp, mm_tmp;
        day_now = rd_utc_ms_to_local_day(now, RD_TZ_OFFSET_MINUTES);
        rd_utc_ms_to_dt(now, RD_TZ_OFFSET_MINUTES, &yr_now, &mo_tmp,
                        &dy_tmp, &hh_tmp, &mm_tmp);
    }

    /* Visible events = ongoing + future, sorted by start (cache order). */
    const lma_cal_event_t *vis[LMA_PB_EVENT_MAX];
    size_t nvis = 0;
    for (size_t i = 0; i < cache->count; i++) {
        const lma_cal_event_t *ev = &cache->events[i];
        if (now != 0 && ev->end_ms != 0 && ev->end_ms < now) {
            continue;   /* past: a family-calendar panel lists what's next */
        }
        vis[nvis++] = ev;
    }
    if (nvis == 0) {
        set_font(u8g2, RD_FONT_UI);
        draw_centered(u8g2, (H / 2) + 7, "no upcoming events");
        u8g2_SendBuffer(u8g2);
        rd_shot_dump(u8g2);
        return;
    }

    draw_headline(u8g2, cache, vis[0], day_now, yr_now);

    /* Compact rows below the picture, at the 9x15B claw-density face.  How
     * many fit is computed from the measured line height. */
    set_font(u8g2, RD_FONT_ROW);
    const int row_asc = u8g2_GetAscent(u8g2);
    const int row_desc = u8g2_GetDescent(u8g2);
    const int row_line_h = row_asc - row_desc;
    rd_row_t rows[RD_MAX_EVENT_ROWS];
    const size_t nrows = rd_layout_rows(RD_ROWS_FIRST_BASE, RD_ROWS_BOTTOM_LIMIT,
                                        row_line_h, row_desc,
                                        RD_MAX_EVENT_ROWS - 1, rows,
                                        RD_MAX_EVENT_ROWS - 1);

    size_t rendered = 1;   /* the headline */
    for (size_t v = 1, r = 0; v < nvis && r < nrows; v++, r++) {
        const lma_cal_event_t *ev = vis[v];
        char meta[32];
        rd_format_event_meta(ev, RD_TZ_OFFSET_MINUTES, day_now, yr_now,
                             meta, sizeof(meta));
        draw_right(u8g2, RD_RIGHT_X, rows[r].baseline, meta);
        const int meta_w = u8g2_GetStrWidth(u8g2, meta);
        const int avail = RD_RIGHT_X - RD_ROWS_LEFT_X - meta_w -
                          RD_ROWS_META_MARGIN;
        draw_clipped(u8g2, RD_ROWS_LEFT_X, rows[r].baseline, ev->title, avail);
        rendered++;
    }

    u8g2_SendBuffer(u8g2);
    rd_shot_dump(u8g2);
    ESP_LOGI(TAG, "rendered %u of %u cached (%u visible, %u row slots; row "
                  "asc=%d desc=%d) headline uid=%s tile=%s weather now=%s "
                  "headline=%s",
             (unsigned)rendered, (unsigned)cache->count, (unsigned)nvis,
             (unsigned)nrows, row_asc, row_desc, vis[0]->uid,
             cache->pic_data_len ? "yes" : "no",
             current->present ? "yes" : "no",
             vis[0]->weather.present ? "yes" : "no");
}

bool rd_render_init(void)
{
    u8g2_st7305_config_t cfg = u8g2_st7305_default_config();
    cfg.mosi_io = RD_MOSI_PIN;
    cfg.sclk_io = RD_SCK_PIN;
    cfg.dc_io = RD_DC_PIN;
    cfg.cs_io = RD_CS_PIN;
    cfg.reset_io = RD_RST_PIN;
    cfg.rotation = U8G2_R1;             /* 400x300 logical landscape */
    cfg.tile_buf_height = U8G2_ST7305_TILE_BUF_FULL;
    cfg.prefer_psram = true;
    esp_err_t err = u8g2_st7305_init(&s_display, &cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "display init failed: %s", esp_err_to_name(err));
        return false;
    }
    return true;
}
