#pragma once

/* ST7789 panel driver for the Cardputer ADV's 1.14" 240x135 IPS display.
 *
 * The display is on its own SPI bus (FSPI / SPI2_HOST) — separate from the
 * LoRa radio (HSPI / SPI3_HOST) — per the Cardputer ADV schematic v1.0 and
 * Meshtastic's variant.h: SCK=36, MOSI=35, DC=34, CS=37, RST=33, backlight=38.
 *
 * Uses ESP-IDF's esp_lcd component (esp_lcd_panel_st7789 is built into IDF);
 * the panel is 240x320 GRAM with the visible 240x135 window offset by
 * (52,40) before the landscape rotation — the (40,53) gap in the rotated
 * frame matches M5Stack's M5GFX config.  invert=true (ST7789V2).
 *
 * Implements cardputer::chart::ChartDisplay so the same chart renderer that
 * runs on the host (against a fake display) drives the real panel. */

#include <cstdint>
#include "chart.h"

namespace Cardputer::display {

    class St7789 : public cardputer::chart::ChartDisplay {
    public:
        /* Initialise the SPI bus + panel + backlight.  Returns true on
         * success.  Idempotent. */
        bool init();

        // cardputer::chart::ChartDisplay overrides
        void fill(uint32_t rgb888) override;
        void pixel(int x, int y, uint32_t rgb888) override;
        void line(int x0, int y0, int x1, int y1, uint32_t rgb888) override;
        void text(const char* s, int x, int y, uint32_t rgb888) override;

        // RGB888 → RGB565 helper (shared by paint calls)
        static uint16_t rgb565(uint32_t rgb888);

    private:
        static constexpr int LCD_H_RES = 240;

        void paint_pixel(int x, int y, uint16_t color565);
        void text_glyph(int x, int y, uint8_t c, uint32_t rgb888);
        void blit(int x, int y, int w, int h, const uint16_t* buf);

        // One screen row of RGB565 — kept in the object (BSS, the display is a
        // static instance) so fill() never burns the already-slim 8 KB main
        // task stack with a per-call 240×N buffer.
        uint16_t _row[LCD_H_RES];

        void* _panel = nullptr;   // esp_lcd_panel_handle_t (opaque)
        bool  _on   = false;
    };

}
