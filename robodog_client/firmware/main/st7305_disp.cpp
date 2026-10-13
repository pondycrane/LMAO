// Port of robodog_client/st7305.py — see that file for the buffer contract:
// row-major 1-bit frame, ROW_BYTES(38) bytes per row, bit 7 = leftmost pixel,
// re-mapped into the ST7305's 8x8-tile column format via the 4x4 LUT.
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

constexpr int kWidth = 300;
constexpr int kHeight = 400;
constexpr int kRowBytes = (kWidth + 7) / 8;  // 38
constexpr int kTilesY = kHeight / 8;         // 50 eight-row bands
constexpr int kTilesX = 38;                  // 38 eight-column tiles per band

// LVGL 9 I1 draw buffers begin with an 8-byte palette (2 x ARGB8888); the
// pixel data (row-major, MSB = leftmost, stride kRowBytes) follows it.
constexpr size_t kI1PaletteBytes = 8;
constexpr size_t kFrameBytes = kRowBytes * kHeight;  // 15200

spi_device_handle_t s_spi = nullptr;
lv_display_t* s_disp = nullptr;

// Static, no malloc: full-screen LVGL draw buffer + panel staging buffers.
uint8_t s_draw_buf[kI1PaletteBytes + kFrameBytes];
uint8_t s_row_base[kWidth];   // U8G2-style column bytes for one band
uint8_t s_all_rows[300];      // one band's windowed 0x2C payload (<= 300)

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

// ---- init (Waveshare u8g2_st7305.c st7305_full_init, verbatim) ----------
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

// ---- full-frame refresh --------------------------------------------------

// The 4x4 LUT from u8g2_st7305.c: maps 2-bit pixel taps to panel nibbles.
constexpr uint8_t kLut[4][4] = {
    {0x00, 0x80, 0x40, 0xC0},
    {0x00, 0x20, 0x10, 0x30},
    {0x00, 0x08, 0x04, 0x0C},
    {0x00, 0x02, 0x01, 0x03},
};

// Build U8G2-style column bytes (8 vertical pixels each, bit r = row offset
// in the band) for the band's pixel columns [col, col+cols).
void column_bytes(const uint8_t* frame, int y_band, int col, int cols,
                  uint8_t* out) {
    const int base_row = y_band * 8;
    for (int i = 0; i < cols; i++) {
        const int c = col + i;
        const int b = c >> 3;
        const uint8_t mask = (uint8_t)(0x80 >> (c & 7));  // MSB = leftmost
        uint8_t v = 0;
        for (int r = 0; r < 8; r++) {
            if (frame[(base_row + r) * kRowBytes + b] & mask) {
                v |= (uint8_t)(1 << r);
            }
        }
        out[i] = v;
    }
}

// Port of U8G2_MSG_DISPLAY_DRAW_TILE for one 8-row band of `cnt`
// eight-column tiles starting at tile x_pos. s_row_base spans the full panel
// width (300 column bytes), indexed by the absolute pixel column exactly as
// the C driver does.
void draw_band(const uint8_t* frame, int y_pos, int x_pos, int cnt) {
    const int first_col = x_pos * 8;
    int last_col = (x_pos + cnt) * 8 - 1;
    if (last_col >= kWidth) last_col = kWidth - 1;

    const int addr_start = 0x12 + first_col / 12;
    const int addr_end = 0x12 + last_col / 12;
    const int send_cnt = (addr_end - addr_start + 1) * 3;

    const int addr_first_col = (addr_start - 0x12) * 12;
    int addr_last_col = (addr_end - 0x12) * 12 + 11;
    if (addr_last_col >= kWidth) addr_last_col = kWidth - 1;

    // U8G2-style column bytes for every pixel column (absolute index).
    column_bytes(frame, y_pos, 0, kWidth, s_row_base);

    memset(s_all_rows, 0, (size_t)send_cnt * 4);
    for (int sr = 0; sr < 4; sr++) {
        const int shift = sr * 2;
        const int base_off = sr * send_cnt;
        int j = 0;
        for (int col = addr_first_col; col <= addr_last_col; col += 4, j++) {
            s_all_rows[base_off + j] =
                (uint8_t)(kLut[0][(s_row_base[col] >> shift) & 3] |
                          kLut[1][(s_row_base[col + 1] >> shift) & 3] |
                          kLut[2][(s_row_base[col + 2] >> shift) & 3] |
                          kLut[3][(s_row_base[col + 3] >> shift) & 3]);
        }
    }

    const uint8_t col_bounds[2] = {(uint8_t)(0x3C - addr_end),
                                   (uint8_t)(0x3C - addr_start)};
    const uint8_t row_bounds[2] = {(uint8_t)(y_pos * 4),
                                   (uint8_t)(y_pos * 4 + 3)};
    panel_cmd_data(0x2A, col_bounds, 2);
    panel_cmd_data(0x2B, row_bounds, 2);
    panel_cmd_data(0x2C, s_all_rows, (size_t)send_cnt * 4);
}

// Blit a row-major 1-bit frame (kFrameBytes) to the panel.
void panel_show(const uint8_t* frame) {
    if (frame == nullptr) return;  // RLCD holds the last frame when powered down
    for (int y_band = 0; y_band < kTilesY; y_band++) {
        draw_band(frame, y_band, 0, kTilesX);
    }
}

// ---- LVGL glue -----------------------------------------------------------

void flush_cb(lv_display_t* disp, const lv_area_t* area, uint8_t* px_map) {
    (void)area;  // RLCD: always push the full frame; in DIRECT mode LVGL
                 // hands us the whole panel anyway.
    // px_map points at the I1 palette; pixel data starts after it. LVGL 9 I1
    // packs pixels MSB-first per byte (bit 7 = leftmost), matching the
    // frame contract of the reference driver (bit set = ink pixel).
    panel_show(px_map + kI1PaletteBytes);
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

    // SPI master, 24 MHz mode 0 MSB-first (as the reference driver).
    spi_bus_config_t bus_cfg = {};
    bus_cfg.sclk_io_num = kPinSck;
    bus_cfg.mosi_io_num = kPinMosi;
    bus_cfg.miso_io_num = -1;
    bus_cfg.quadwp_io_num = -1;
    bus_cfg.quadhd_io_num = -1;
    bus_cfg.max_transfer_sz = 512;
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

    // LVGL: I1 (1-bit, MSB-first) display with a static full-screen buffer in
    // DIRECT render mode — RLCDs always get a full-frame push.
    lv_init();
    lv_tick_set_cb(tick_cb);
    s_disp = lv_display_create(kWidth, kHeight);
    if (s_disp == nullptr) {
        ESP_LOGE(kTag, "lv_display_create failed");
        return false;
    }
    lv_display_set_color_format(s_disp, LV_COLOR_FORMAT_I1);
    if (lv_draw_buf_width_to_stride(kWidth, LV_COLOR_FORMAT_I1) !=
        (uint32_t)kRowBytes) {
        ESP_LOGE(kTag, "unexpected I1 stride");
        s_disp = nullptr;
        return false;
    }
    // NOTE: portrait for now. A rotated logical (400x300) landscape view would
    // need LVGL's matrix-rotation path (LV_USE_MATRIX +
    // LV_DRAW_TRANSFORM_USE_MATRIX + lv_display_set_matrix_rotation), which
    // changes the flush area/stride contract in direct mode and requires a
    // transpose-aware flush (WIP in the follow-up). Disabled: reverted.
    lv_display_set_flush_cb(s_disp, flush_cb);
    lv_display_set_buffers(s_disp, s_draw_buf, nullptr, sizeof(s_draw_buf),
                           LV_DISPLAY_RENDER_MODE_DIRECT);
    ESP_LOGI(kTag, "st7305 display up (%dx%d, %u-byte frame)", kWidth, kHeight,
             (unsigned)kFrameBytes);
    return true;
}

lv_display_t* st7305_disp_get(void) { return s_disp; }
