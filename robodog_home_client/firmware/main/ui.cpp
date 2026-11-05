// Panel rendering for the RoboDog home client (see ui.h).
//
// Landscape 400x300 canvas (u8g2 U8G2_R1 over the ST7305):
//   row 1  device name (left) + latest temp/humidity (right)
//   row 2  status hint (left) + sample count (right)
//   chart  temp trace (tenths of degC) and humidity trace (%), each scaled to
//          its OWN extent, with min/max labels on the axes and the current
//          value at the end of each line ("30.4°C" / "43%")
//   x axis clock times under the left edge, middle and right edge ("now")
#include "ui.h"

#include <cstdio>
#include <cstring>

#include "esp_log.h"

#include "config.h"
#include "rd_chart.h"
#include "u8g2_st7305.h"

static const char* TAG = "ui";

namespace {

u8g2_st7305_t s_dev;
bool s_ready = false;

constexpr int W = 400;
constexpr int H = 300;

// Plot box: left gutter for temperature labels, right gutter for humidity.
constexpr int PLOT_X0 = 46;
constexpr int PLOT_Y0 = 66;
constexpr int PLOT_X1 = 352;
constexpr int PLOT_Y1 = 250;

void set_font(u8g2_t* u8g2, const uint8_t* font) {
    u8g2_SetFont(u8g2, font);
    u8g2_SetFontRefHeightAll(u8g2);
}

void draw_right(u8g2_t* u8g2, int right_x, int baseline, const char* s) {
    u8g2_DrawStr(u8g2, right_x - u8g2_GetStrWidth(u8g2, s), baseline, s);
}

void rd_shot_dump(u8g2_t* u8g2) {
    const uint8_t* buf = u8g2_GetBufferPtr(u8g2);
    const size_t n = u8g2_GetBufferSize(u8g2);
    printf("\nRD_SHOT_BEGIN bufsize=%u w=%d h=%d\n", (unsigned)n,
           u8g2_GetDisplayWidth(u8g2), u8g2_GetDisplayHeight(u8g2));
    for (size_t i = 0; i < n; i++) {
        printf("%02x", buf[i]);
        if ((i & 0x1f) == 0x1f) printf("\n");
    }
    printf("\nRD_SHOT_END\n");
    fflush(stdout);
}

// "22.6" from tenths of a degree.
void fmt_temp(char* out, size_t cap, uint32_t c10) {
    snprintf(out, cap, "%u.%u", (unsigned)(c10 / 10), (unsigned)(c10 % 10));
}

void draw_trace(u8g2_t* u8g2, const rd_point_t* pts, size_t n, bool dotted) {
    for (size_t i = 1; i < n; i++) {
        if (dotted && (i & 1)) {
            u8g2_DrawPixel(u8g2, pts[i].x, pts[i].y);
            continue;
        }
        u8g2_DrawLine(u8g2, pts[i - 1].x, pts[i - 1].y, pts[i].x, pts[i].y);
    }
}

/* The "now" endpoint marker.  Drawn AFTER the end-of-line labels, which knock
 * a paper-coloured box out of the trace (the marker would otherwise be erased
 * by that box where it overlaps). */
void draw_marker(u8g2_t* u8g2, const rd_point_t* pts, size_t n) {
    if (n > 0) {
        u8g2_DrawBox(u8g2, pts[n - 1].x - 2, pts[n - 1].y - 2, 5, 5);
    }
}

int clamp_int(int v, int lo, int hi) {
    return v < lo ? lo : (v > hi ? hi : v);
}

/* Degree sign only when the 6x10 face actually carries it; otherwise plain
 * "C".  NOTE: the escape must not be followed by a hex digit ("\xB0C" would
 * parse as one long hex escape), hence the split literal. */
const char* deg_suffix(u8g2_t* u8g2) {
    return u8g2_IsGlyph(u8g2, 0x00B0) ? "\xB0" "C" : "C";
}

/* Value label pinned to the END of a trace (right edge = "now").
 *
 * The two lines cross and share a plot box, so the reading at the end of each
 * one — with its unit — is what tells them apart: "30.4°C" vs "43%".  The box
 * behind the text is filled with PAPER colour first, so the trace cannot
 * strike through the digits. */
void draw_end_label(u8g2_t* u8g2, int right_x, int baseline, const char* s) {
    const int w = u8g2_GetStrWidth(u8g2, s);
    const int x = right_x - w;
    u8g2_SetDrawColor(u8g2, 1);  // paper
    u8g2_DrawBox(u8g2, x - 2, baseline - 8, w + 4, 11);
    u8g2_SetDrawColor(u8g2, 0);  // ink
    u8g2_DrawStr(u8g2, x, baseline, s);
}

void draw_axes(u8g2_t* u8g2, const rd_chart_plan_t* plan, size_t samples) {
    u8g2_DrawFrame(u8g2, PLOT_X0, PLOT_Y0, PLOT_X1 - PLOT_X0 + 1,
                   PLOT_Y1 - PLOT_Y0 + 1);
    char buf[16];

    set_font(u8g2, u8g2_font_6x10_tf);
    if (plan->temp_n > 0) {
        fmt_temp(buf, sizeof(buf), plan->temp_max);
        u8g2_DrawStr(u8g2, 2, PLOT_Y0 + 8, buf);
        fmt_temp(buf, sizeof(buf), plan->temp_min);
        u8g2_DrawStr(u8g2, 2, PLOT_Y1, buf);
        u8g2_DrawStr(u8g2, 2, PLOT_Y1 + 12, "C");
    }
    if (plan->hum_n > 0) {
        snprintf(buf, sizeof(buf), "%u%%", (unsigned)plan->hum_max);
        u8g2_DrawStr(u8g2, PLOT_X1 + 6, PLOT_Y0 + 8, buf);
        snprintf(buf, sizeof(buf), "%u%%", (unsigned)plan->hum_min);
        u8g2_DrawStr(u8g2, PLOT_X1 + 6, PLOT_Y1, buf);
    }

    /* Time axis: a tick under the left edge, the middle sample and the right
     * edge ("now"), each labelled with the local clock time of that sample
     * (ChartBundle.start_ms + i*period_ms, offset by RH_TZ_OFFSET_MINUTES). */
    char labels[RD_CHART_TICKS][RD_CHART_LABEL_LEN];
    rd_chart_time_labels(plan, samples, RH_TZ_OFFSET_MINUTES, labels);
    const size_t idx[RD_CHART_TICKS] = {0, samples / 2, samples ? samples - 1 : 0};
    const int label_base = PLOT_Y1 + 15;
    for (int k = 0; k < RD_CHART_TICKS; k++) {
        const int x = rd_chart_x(plan, idx[k], samples);
        u8g2_DrawVLine(u8g2, x, PLOT_Y1 + 1, 4);  /* tick mark below the axis */
        if (k == 0) {
            u8g2_DrawStr(u8g2, x, label_base, labels[k]);
        } else if (k == RD_CHART_TICKS - 1) {
            draw_right(u8g2, x, label_base, labels[k]);
        } else {
            const int w = u8g2_GetStrWidth(u8g2, labels[k]);
            u8g2_DrawStr(u8g2, x - w / 2, label_base, labels[k]);
        }
    }
}

}  // namespace

namespace ui {

bool init(void) {
    u8g2_st7305_config_t cfg = u8g2_st7305_default_config();
    cfg.mosi_io = RH_MOSI_PIN;
    cfg.sclk_io = RH_SCK_PIN;
    cfg.dc_io = RH_DC_PIN;
    cfg.cs_io = RH_CS_PIN;
    cfg.reset_io = RH_RST_PIN;
    cfg.rotation = U8G2_R1;  // 400x300 logical landscape
    cfg.tile_buf_height = U8G2_ST7305_TILE_BUF_FULL;
    cfg.prefer_psram = true;
    const esp_err_t err = u8g2_st7305_init(&s_dev, &cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "display init failed: %s", esp_err_to_name(err));
        return false;
    }
    s_ready = true;
    return true;
}

void render(const char* device_name, const lma_chart::ChartBundle* bundle,
            bool have_data, bool stale, const char* status) {
    if (!s_ready) return;
    u8g2_t* u8g2 = u8g2_st7305_get_u8g2(&s_dev);

    u8g2_ClearBuffer(u8g2);
    // ST7305 is a reflective panel: clear fills black, so paint the whole
    // canvas white first and draw all content in black ink (same convention as
    // robodog_client/main/rd_render.c).
    const int W_disp = u8g2_GetDisplayWidth(u8g2);
    const int H_disp = u8g2_GetDisplayHeight(u8g2);
    u8g2_SetDrawColor(u8g2, 1);
    u8g2_DrawBox(u8g2, 0, 0, W_disp, H_disp);
    u8g2_SetDrawColor(u8g2, 0);

    char line[64];
    set_font(u8g2, u8g2_font_10x20_tf);
    snprintf(line, sizeof(line), "%s", device_name && device_name[0]
                                            ? device_name
                                            : "LMAO env");
    u8g2_DrawStr(u8g2, 4, 22, line);

    char right[48] = "";
    if (have_data && bundle != nullptr) {
        char t[16] = "", h[16] = "";
        if (!bundle->temp.empty()) {
            fmt_temp(t, sizeof(t), bundle->temp.back());
        }
        if (!bundle->hum.empty()) {
            snprintf(h, sizeof(h), "%u%%", (unsigned)bundle->hum.back());
        }
        if (t[0] && h[0]) snprintf(right, sizeof(right), "%sC %s", t, h);
        else if (t[0]) snprintf(right, sizeof(right), "%sC", t);
        else if (h[0]) snprintf(right, sizeof(right), "%s", h);
    }
    if (right[0]) draw_right(u8g2, W - 4, 22, right);

    set_font(u8g2, u8g2_font_6x10_tf);
    snprintf(line, sizeof(line), "%s%s", stale ? "stale · " : "",
             status ? status : "");
    u8g2_DrawStr(u8g2, 4, 40, line);

    char counts[48] = "";
    if (have_data && bundle != nullptr) {
        snprintf(counts, sizeof(counts), "%u samples",
                 (unsigned)bundle->temp.size());
    }
    if (counts[0]) draw_right(u8g2, W - 4, 40, counts);

    rd_chart_plan_t plan;
    if (have_data && bundle != nullptr) {
        rd_chart_plan(bundle->temp.data(), bundle->temp.size(),
                      bundle->hum.data(), bundle->hum.size(), PLOT_X0,
                      PLOT_Y0, PLOT_X1, PLOT_Y1, &plan);
        const size_t full = bundle->temp.size() > bundle->hum.size()
                                ? bundle->temp.size()
                                : bundle->hum.size();
        rd_chart_set_time(&plan, bundle->start_ms, bundle->period_ms, full);
        const size_t samples = plan.temp_n > plan.hum_n ? plan.temp_n : plan.hum_n;
        draw_axes(u8g2, &plan, samples);
        draw_trace(u8g2, plan.temp, plan.temp_n, /*dotted=*/false);
        draw_trace(u8g2, plan.hum, plan.hum_n, /*dotted=*/true);

        // Value at the end of each line.  Positioned just above the temp
        // endpoint and just below the humidity one; when the two endpoints are
        // close, the labels are pushed apart so neither is lost.
        char temp_lbl[16] = "";
        char hum_lbl[16] = "";
        int temp_base = 0;
        int hum_base = 0;
        const int label_lo = PLOT_Y0 + 9;
        const int label_hi = PLOT_Y1 - 1;
        if (plan.temp_n > 0) {
            fmt_temp(temp_lbl, sizeof(temp_lbl), bundle->temp.back());
            const char* suffix = deg_suffix(u8g2);
            strncat(temp_lbl, suffix,
                    sizeof(temp_lbl) - strlen(temp_lbl) - 1);
            temp_base = clamp_int(plan.temp[plan.temp_n - 1].y - 3,
                                  label_lo, label_hi);
        }
        if (plan.hum_n > 0) {
            snprintf(hum_lbl, sizeof(hum_lbl), "%u%%",
                     (unsigned)bundle->hum.back());
            hum_base = clamp_int(plan.hum[plan.hum_n - 1].y + 10,
                                 label_lo, label_hi);
        }
        if (temp_lbl[0] && hum_lbl[0] && hum_base - temp_base < 12) {
            hum_base = clamp_int(temp_base + 12, label_lo, label_hi);
            if (hum_base - temp_base < 12) {
                temp_base = clamp_int(hum_base - 12, label_lo, label_hi);
            }
        }
        if (temp_lbl[0]) draw_end_label(u8g2, PLOT_X1 - 3, temp_base, temp_lbl);
        if (hum_lbl[0]) draw_end_label(u8g2, PLOT_X1 - 3, hum_base, hum_lbl);

        draw_marker(u8g2, plan.temp, plan.temp_n);
        draw_marker(u8g2, plan.hum, plan.hum_n);
    } else {
        u8g2_DrawFrame(u8g2, PLOT_X0, PLOT_Y0, PLOT_X1 - PLOT_X0 + 1,
                       PLOT_Y1 - PLOT_Y0 + 1);
        u8g2_DrawStr(u8g2, PLOT_X0 + 60, (PLOT_Y0 + PLOT_Y1) / 2,
                     "waiting for data...");
    }

    u8g2_SendBuffer(u8g2);
    rd_shot_dump(u8g2);
}

}  // namespace ui
