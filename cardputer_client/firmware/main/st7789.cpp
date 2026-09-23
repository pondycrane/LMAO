#include "st7789.h"

#include <cstring>

#include "esp_heap_caps.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_log.h"
#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "font8x8.h"

static const char* TAG = "st7789";

namespace Cardputer::display {

    namespace {
        constexpr int PIN_DC  = 34;
        constexpr int PIN_CS  = 37;
        constexpr int PIN_RST = 33;
        constexpr int PIN_SCK = 36;
        constexpr int PIN_MOSI = 35;
        constexpr int PIN_BL  = 38;   // backlight (also RGB LED power rail)

        constexpr spi_host_device_t HOST = SPI2_HOST;  // display bus; LoRa owns SPI3_HOST
        constexpr int PCLK_HZ = 40 * 1000 * 1000;
        constexpr int H_RES = 240;
        constexpr int V_RES = 135;

        // ST7789V2 on the Cardputer ADV — visible 240x135 window in the
        // 240x320 GRAM, landscape: gap (40,53) matches M5Stack's M5GFX geometry.
        constexpr int GAP_X = 40;
        constexpr int GAP_Y = 53;
    }

    uint16_t St7789::rgb565(uint32_t rgb888) {
        uint8_t r = (rgb888 >> 16) & 0xFF;
        uint8_t g = (rgb888 >> 8) & 0xFF;
        uint8_t b = rgb888 & 0xFF;
        return (uint16_t)(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3));
    }

    bool St7789::init() {
        if (_on) return true;

        // Backlight: drive HIGH (Cardputer ADV shares G38 with RGB LED power).
        gpio_config_t bl = {
            .pin_bit_mask = 1ULL << PIN_BL,
            .mode = GPIO_MODE_OUTPUT,
        };
        gpio_config(&bl);
        gpio_set_level((gpio_num_t)PIN_BL, 1);

        // FSPI bus for the panel (separate instance from RadioLib's SPI3).
        spi_bus_config_t bus = {};
        bus.sclk_io_num = PIN_SCK;
        bus.mosi_io_num = PIN_MOSI;
        bus.miso_io_num = -1;
        bus.quadwp_io_num = -1;
        bus.quadhd_io_num = -1;
        bus.max_transfer_sz = H_RES * V_RES * sizeof(uint16_t) + 8;
        esp_err_t err = spi_bus_initialize(HOST, &bus, SPI_DMA_CH_AUTO);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "spi_bus_initialize failed: 0x%x", err);
            return false;
        }

        esp_lcd_panel_io_handle_t io = nullptr;
        esp_lcd_panel_io_spi_config_t io_cfg = {};
        io_cfg.dc_gpio_num = PIN_DC;
        io_cfg.cs_gpio_num = PIN_CS;
        io_cfg.pclk_hz = PCLK_HZ;
        io_cfg.lcd_cmd_bits = 8;
        io_cfg.lcd_param_bits = 8;
        io_cfg.spi_mode = 0;
        io_cfg.trans_queue_depth = 10;
        err = esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)HOST, &io_cfg, &io);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "panel io failed: 0x%x", err);
            return false;
        }

        esp_lcd_panel_dev_config_t panel_cfg = {};
        panel_cfg.reset_gpio_num = PIN_RST;
        panel_cfg.rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB;
        panel_cfg.bits_per_pixel = 16;
        esp_err_t perr = esp_lcd_new_panel_st7789(io, &panel_cfg, (esp_lcd_panel_handle_t*)&_panel);
        if (perr != ESP_OK) {
            ESP_LOGE(TAG, "panel create failed: 0x%x", perr);
            return false;
        }
        esp_lcd_panel_handle_t panel = (esp_lcd_panel_handle_t)_panel;

        esp_lcd_panel_reset(panel);
        esp_lcd_panel_init(panel);
        esp_lcd_panel_disp_on_off(panel, true);
        // 240x135 landscape: same geometry M5Stack's M5GFX uses for this panel.
        esp_lcd_panel_swap_xy(panel, true);
        esp_lcd_panel_mirror(panel, true, false);
        esp_lcd_panel_set_gap(panel, GAP_X, GAP_Y);
        esp_lcd_panel_invert_color(panel, true);

        _on = true;
        ESP_LOGI(TAG, "ST7789 ready (240x135 @40MHz, SPI2_HOST)");
        return true;
    }

    void St7789::blit(int x, int y, int w, int h, const uint16_t* buf) {
        if (!_on || !_panel) return;
        esp_lcd_panel_draw_bitmap((esp_lcd_panel_handle_t)_panel, x, y, x + w, y + h, buf);
    }

    void St7789::fill(uint32_t rgb888) {
        uint16_t c = rgb565(rgb888);
        // Row buffer lives in the object (BSS) to keep the main-task stack
        // safe — this fills one 240px row per SPI transaction (135 calls).
        for (int i = 0; i < LCD_H_RES; i++) _row[i] = c;
        for (int y = 0; y < V_RES; y++) blit(0, y, LCD_H_RES, 1, _row);
    }

    void St7789::paint_pixel(int x, int y, uint16_t color565) {
        if (x < 0 || x >= H_RES || y < 0 || y >= V_RES) return;
        blit(x, y, 1, 1, &color565);
    }

    void St7789::pixel(int x, int y, uint32_t rgb888) {
        paint_pixel(x, y, rgb565(rgb888));
    }

    void St7789::line(int x0, int y0, int x1, int y1, uint32_t rgb888) {
        uint16_t c = rgb565(rgb888);
        // Bresenham — identical to chart.cpp/_line and chart.py.
        int dx = std::abs(x1 - x0);
        int dy = -std::abs(y1 - y0);
        int sx = (x0 < x1) ? 1 : -1;
        int sy = (y0 < y1) ? 1 : -1;
        int err = dx + dy;
        for (;;) {
            paint_pixel(x0, y0, c);
            if (x0 == x1 && y0 == y1) return;
            int e2 = 2 * err;
            if (e2 >= dy) { err += dy; x0 += sx; }
            if (e2 <= dx) { err += dx; y0 += sy; }
        }
    }

    void St7789::text(const char* s, int x, int y, uint32_t rgb888) {
        if (!s) return;
        uint16_t c = rgb565(rgb888);
        int cx = x;
        int cy = y;
        for (const char* p = s; *p && cx < H_RES; p++) {
            uint8_t ch = (uint8_t)*p;
            if (ch == '\n') { cx = x; cy += 9; continue; }
            if (ch < 0x20 || ch > 0x7E) { cx += 9; continue; }
            const uint8_t* glyph = kFont8x8[ch - 0x20];
            for (int gy = 0; gy < 8; gy++) {
                uint8_t row = glyph[gy];
                for (int gx = 0; gx < 8; gx++) {
                    if (row & (0x80 >> gx)) paint_pixel(cx + gx, cy + gy, c);
                }
            }
            cx += 9;   // 1px spacing
        }
    }

}
