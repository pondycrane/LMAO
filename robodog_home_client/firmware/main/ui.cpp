// Panel rendering for the RoboDog home client (see ui.h).
//
// Landscape 400x300 canvas (u8g2 U8G2_R1 over the ST7305):
//   row 1  device name (left) + latest temp/humidity (right)
//   row 2  status hint (left) + sample count (right)
//   chart  temp trace (tenths of degC) and humidity trace (%), each scaled to
//          its OWN extent, with min/max labels on the axes
//   footer "oldest" ... "now"
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
    if (n > 0) {
        u8g2_DrawBox(u8g2, pts[n - 1].x - 2, pts[n - 1].y - 2, 5, 5);
    }
}

void draw_axes(u8g2_t* u8g2, const rd_chart_plan_t* plan) {
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
    u8g2_DrawStr(u8g2, PLOT_X0, H - 4, "oldest");
    draw_right(u8g2, PLOT_X1, H - 4, "now");
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
        draw_axes(u8g2, &plan);
        draw_trace(u8g2, plan.temp, plan.temp_n, /*dotted=*/false);
        draw_trace(u8g2, plan.hum, plan.hum_n, /*dotted=*/true);
        u8g2_DrawStr(u8g2, PLOT_X0 + 4, PLOT_Y0 + 12, "T");
        u8g2_DrawStr(u8g2, PLOT_X0 + 16, PLOT_Y0 + 12, "H");
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
