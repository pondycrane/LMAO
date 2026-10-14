// st7305_disp.cpp — ST7305 400x300 reflective LCD driver + LVGL 9 display glue
// for the Waveshare ESP32-S3 RLCD-4.2 board, landscape (horizontal).
//
// Canvas: the reference RLCD-4.2 demo drives this exact panel horizontally
// with a direct landscape pixel map into the ST7305's 2x4 grid channel then a
// single full-frame 0x2C burst (RLCD_SetLandscapePixel / RLCD_Display). We
// replicate that canvas verbatim (same init table, same window registers,
// same landscape mapping, same burst) — no u8g2-style tile transposition.
//
// LVGL logical surface: 400x300 landscape, LV_COLOR_FORMAT_I1 (1-bit,
// MSB-first, bit set = ink). flush_cb remaps ink pixels into the landscape
// grid and pushes the whole frame (RLCDs are full-frame refresh).
#include "st7305_disp.h"

#include <string.h>

#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_err.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

namespace {

constexpr char kTag[] = "st7305";

// Pins from the board schematic / Waveshare u8g2_st7305 user_config.h:
// DC(PIN5) CS(PIN40) SCK(PIN11) MOSI(PIN12) RST(PIN41) TE(PIN6)
constexpr gpio_num_t kPinDc = GPIO_NUM_5;
constexpr gpio_num_t kPinCs = GPIO_NUM_40;
constexpr gpio_num_t kPinSck = GPIO_NUM_11;
constexpr gpio_num_t kPinMosi = GPIO_NUM_12;
constexpr gpio_num_t kPinRst = GPIO_NUM_41;
constexpr gpio_num_t kPinTe = GPIO_NUM_6;  // tearing effect; input, unused

constexpr spi_host_device_t kSpiHost = SPI2_HOST;
constexpr int kSpiClockHz = 24000000;  // 24 MHz, mode 0, MSB first

// --- Logical (landscape) surface LVGL renders into -------------------------
// 400x300 landscape, matching how the reference build runs this panel
// horizontally.
constexpr int kLogW = 400;
constexpr int kLogH = 300;
constexpr int kLogRowBytes = (kLogW + 7) / 8;  // 50
// The ST7305 grid is 15000 bytes: 200 (2-pixel-wide) columns x 75 (4-pixel-
// tall) block-rows — one 0x2C burst per frame.
constexpr size_t kGridBytes = kLogRowBytes * kLogH;  // 15000
// ST7305 landscape block group height (per grid column the panel packs 4
// pixels per byte), matching the reference driver's H4 = height_ >> 2.
constexpr int kBlockRows = kLogH / 4;  // 75

// LVGL 9 I1 draw buffers begin with an 8-byte palette (2 x ARGB8888); the
// pixel data (row-major, MSB = leftmost, stride kLogRowBytes) follows it.
constexpr size_t kI1PaletteBytes = 8;

spi_device_handle_t s_spi = nullptr;
lv_display_t* s_disp = nullptr;

// Static, no malloc: full-screen landscape LVGL draw buffer + the ST7305 grid
// staging buffer the flush builds before the 0x2C burst.
uint8_t s_draw_buf[kI1PaletteBytes + kGridBytes];
uint8_t s_grid[kGridBytes];

void delay_ms(uint32_t ms) { vTaskDelay(pdMS_TO_TICKS(ms)); }

void spi_write(const uint8_t* data, size_t len) {
    spi_transaction_t t = {};
    t.length = (uint32_t)(len * 8);
    t.tx_buffer = data;
    esp_err_t err = spi_device_transmit(s_spi, &t);  // blocking
    if (err != ESP_OK) {
        ESP_LOGW(kTag, "spi tx failed: %s", esp_err_to_name(err));
    }
}

// CS is toggled manually around each transfer, exactly like the reference
// driver, so command+parameter pairs stay in one asserted window.
void panel_cmd(uint8_t cmd) {
    gpio_set_level(kPinCs, 0);
    gpio_set_level(kPinDc, 0);  // command
    spi_write(&cmd, 1);
    gpio_set_level(kPinCs, 1);
}

void panel_cmd_data(uint8_t cmd, const uint8_t* data, size_t len) {
    gpio_set_level(kPinCs, 0);
    gpio_set_level(kPinDc, 0);
    spi_write(&cmd, 1);
    if (len > 0) {
        gpio_set_level(kPinDc, 1);  // parameter
        spi_write(data, len);
    }
    gpio_set_level(kPinCs, 1);
}

void panel_reset() {
    gpio_set_level(kPinRst, 1);
    delay_ms(50);
    gpio_set_level(kPinRst, 0);
    delay_ms(20);
    gpio_set_level(kPinRst, 1);
    delay_ms(50);
}

// ---- init (verbatim from the reference landscape driver) ------------------
struct InitCmd {
    uint8_t cmd;
    uint8_t data[10];
    uint8_t len;
};

constexpr InitCmd kInitTable[] = {
    {0xD6, {0x17, 0x02}, 2},
    {0xD1, {0x01}, 1},
    {0xC0, {0x11, 0x04}, 2},
    {0xC1, {0x69, 0x69, 0x69, 0x69}, 4},
    {0xC2, {0x19, 0x19, 0x19, 0x19}, 4},
    {0xC4, {0x4B, 0x4B, 0x4B, 0x4B}, 4},
    {0xC5, {0x19, 0x19, 0x19, 0x19}, 4},
    {0xD8, {0x80, 0xE9}, 2},
    {0xB2, {0x02}, 1},
    {0xB3, {0xE5, 0xF6, 0x05, 0x46, 0x77, 0x77, 0x77, 0x77, 0x76, 0x45}, 10},
    {0xB4, {0x05, 0x46, 0x77, 0x77, 0x77, 0x77, 0x76, 0x45}, 8},
    {0x62, {0x32, 0x03, 0x1F}, 3},
    {0xB7, {0x13}, 1},
    {0xB0, {0x64}, 1},
};

constexpr InitCmd kInitTail1[] = {
    {0xC9, {0x00}, 1},
    {0x36, {0x48}, 1},
    {0x3A, {0x11}, 1},
    {0xB9, {0x20}, 1},
    {0xB8, {0x29}, 1},
};

constexpr InitCmd kInitTail2[] = {
    {0x2A, {0x12, 0x2A}, 2},  // column window
    {0x2B, {0x00, 0xC7}, 2},  // row window
    {0x35, {0x00}, 1},
    {0xD0, {0xFF}, 1},
};

void panel_init() {
    panel_reset();
    for (const InitCmd& c : kInitTable) {
        panel_cmd_data(c.cmd, c.data, c.len);
    }
    panel_cmd(0x11);  // sleep out
    delay_ms(120);
    for (const InitCmd& c : kInitTail1) {
        panel_cmd_data(c.cmd, c.data, c.len);
    }
    panel_cmd(0x21);  // invoff
    for (const InitCmd& c : kInitTail2) {
        panel_cmd_data(c.cmd, c.data, c.len);
    }
    panel_cmd(0x38);  // idle off
    panel_cmd(0x29);  // display on
}

// ---- landscape canvas (reference RLCD_Display + RLCD_SetLandscapePixel) ---

// Remap the 400x300 landscape I1 surface into the ST7305 landscape grid.
// Port of the reference driver's RLCD_SetLandscapePixel indexing (its
// AlgorithmOptimization=3 LUT, inlined): each grid byte holds a 2x4 pixel
// block — two neighbouring screen columns x four screen rows —
//     index  = (screen_x >> 1) * kBlockRows + ((299 - screen_y) >> 2)
//     bit    = 7 - (((299 - screen_y) & 3) << 1) | (screen_x & 1))
void landscape_to_grid(const uint8_t* land, uint8_t* grid) {
    memset(grid, 0, kGridBytes);
    for (int sy = 0; sy < kLogH; sy++) {
        const int inv_y = kLogH - 1 - sy;
        const int block_y = inv_y >> 2;
        const int local_y = inv_y & 3;
        for (int sx = 0; sx < kLogW; sx++) {
            const uint8_t* lp = land + sy * kLogRowBytes + (sx >> 3);
            if (*lp & (uint8_t)(0x80 >> (sx & 7))) {
                const int byte_x = sx >> 1;      // 0..199
                const int local_x = sx & 1;
                const uint32_t index = (uint32_t)(byte_x * kBlockRows + block_y);
                const uint8_t mask = (uint8_t)(1u << (7 - ((local_y << 1) | local_x)));
                grid[index] |= mask;
            }
        }
    }
}

// Blast the whole grid to the panel in one 0x2C burst (CS held low for the
// entire DMA write), exactly like the reference RLCD_Display().
void panel_push(const uint8_t* grid) {
    const uint8_t col[] = {0x12, 0x2A};
    const uint8_t row[] = {0x00, 0xC7};
    panel_cmd_data(0x2A, col, 2);
    panel_cmd_data(0x2B, row, 2);
    gpio_set_level(kPinCs, 0);
    { const uint8_t cmd = 0x2C;
      gpio_set_level(kPinDc, 0);
      spi_write(&cmd, 1); }
    gpio_set_level(kPinDc, 1);
    spi_write(grid, kGridBytes);
    gpio_set_level(kPinCs, 1);
}

// ---- LVGL glue -----------------------------------------------------------

void flush_cb(lv_display_t* disp, const lv_area_t* area, uint8_t* px_map) {
    (void)area;  // RLCD: always push the full frame; in DIRECT mode LVGL
                 // hands us the whole panel anyway.
    // px_map points at the I1 palette; pixel data starts after it. LVGL 9 I1
    // packs pixels MSB-first per byte (bit 7 = leftmost).
    landscape_to_grid(px_map + kI1PaletteBytes, s_grid);
    panel_push(s_grid);
    lv_display_flush_ready(disp);
}

uint32_t tick_cb() { return (uint32_t)(esp_timer_get_time() / 1000); }

}  // namespace

bool st7305_disp_init(void) {
    if (s_disp != nullptr) return true;

    // GPIO.
    gpio_config_t out_cfg = {};
    out_cfg.pin_bit_mask = (1ULL << kPinDc) | (1ULL << kPinCs) | (1ULL << kPinRst);
    out_cfg.mode = GPIO_MODE_OUTPUT;
    if (gpio_config(&out_cfg) != ESP_OK) {
        ESP_LOGE(kTag, "gpio_config (outputs) failed");
        return false;
    }
    gpio_config_t te_cfg = {};
    te_cfg.pin_bit_mask = 1ULL << kPinTe;
    te_cfg.mode = GPIO_MODE_INPUT;
    if (gpio_config(&te_cfg) != ESP_OK) {
        ESP_LOGE(kTag, "gpio_config (TE) failed");
        return false;
    }
    gpio_set_level(kPinCs, 1);
    gpio_set_level(kPinDc, 1);
    gpio_set_level(kPinRst, 1);

    // SPI master, 24 MHz mode 0 MSB-first (as the reference driver). One
    // transfer holds an entire frame (the 0x2C burst is kGridBytes).
    spi_bus_config_t bus_cfg = {};
    bus_cfg.sclk_io_num = kPinSck;
    bus_cfg.mosi_io_num = kPinMosi;
    bus_cfg.miso_io_num = -1;
    bus_cfg.quadwp_io_num = -1;
    bus_cfg.quadhd_io_num = -1;
    bus_cfg.max_transfer_sz = kGridBytes;
    esp_err_t err = spi_bus_initialize(kSpiHost, &bus_cfg, SPI_DMA_CH_AUTO);
    if (err != ESP_OK) {
        ESP_LOGE(kTag, "spi_bus_initialize failed: %s", esp_err_to_name(err));
        return false;
    }
    spi_device_interface_config_t dev_cfg = {};
    dev_cfg.clock_speed_hz = kSpiClockHz;
    dev_cfg.mode = 0;
    dev_cfg.spics_io_num = -1;  // CS toggled manually, like the reference driver
    dev_cfg.queue_size = 1;
    err = spi_bus_add_device(kSpiHost, &dev_cfg, &s_spi);
    if (err != ESP_OK) {
        ESP_LOGE(kTag, "spi_bus_add_device failed: %s", esp_err_to_name(err));
        return false;
    }

    panel_init();

    // LVGL: I1 (1-bit, MSB-first) landscape 400x300 display with a static
    // full-screen buffer in DIRECT render mode — RLCDs always get a full-frame
    // push. The flush remaps the landscape surface into the ST7305 grid using
    // the reference landscape canvas (no rotation, no tiling).
    lv_init();
    lv_tick_set_cb(tick_cb);
    s_disp = lv_display_create(kLogW, kLogH);
    if (s_disp == nullptr) {
        ESP_LOGE(kTag, "lv_display_create failed");
        return false;
    }
    lv_display_set_color_format(s_disp, LV_COLOR_FORMAT_I1);
    if (lv_draw_buf_width_to_stride(kLogW, LV_COLOR_FORMAT_I1) !=
        (uint32_t)kLogRowBytes) {
        ESP_LOGE(kTag, "unexpected I1 stride");
        s_disp = nullptr;
        return false;
    }
    lv_display_set_flush_cb(s_disp, flush_cb);
    lv_display_set_buffers(s_disp, s_draw_buf, nullptr, sizeof(s_draw_buf),
                           LV_DISPLAY_RENDER_MODE_DIRECT);
    ESP_LOGI(kTag, "st7305 display up (%dx%d landscape, %u-byte grid)",
             kLogW, kLogH, (unsigned)kGridBytes);
    return true;
}

lv_display_t* st7305_disp_get(void) { return s_disp; }
