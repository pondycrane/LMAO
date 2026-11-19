#include "led_rgb.h"

#include "driver/gpio.h"
#include "driver/rmt.h"
#include "esp_log.h"

namespace lma_led {
namespace {

const char* TAG = "lma_led";

constexpr int kMaxPixels = 8;      // Atom Lite S3 has 4; headroom for variants
constexpr int kRmtClkDiv = 8;      // APB 80 MHz / 8 = 0.1 us per tick

// SK6812 / WS2812 bit timing in 0.1 us ticks.
constexpr uint16_t T1H = 9;
constexpr uint16_t T1L = 3;
constexpr uint16_t T0H = 3;
constexpr uint16_t T0L = 9;
constexpr uint16_t RESET_TICKS = 1000;   // 100 us

bool s_ready = false;
int s_pixels = 0;
int s_channel = 0;
rmt_item32_t s_items[kMaxPixels * 24 + 1];

}  // namespace

bool init(int gpio, int pixel_count, int rmt_channel) {
    if (pixel_count <= 0 || pixel_count > kMaxPixels) {
        ESP_LOGE(TAG, "pixel_count %d out of range 1..%d", pixel_count, kMaxPixels);
        return false;
    }

    gpio_config_t out = {};
    out.pin_bit_mask = 1ULL << gpio;
    out.mode = GPIO_MODE_OUTPUT;
    out.pull_up_en = GPIO_PULLUP_DISABLE;
    out.pull_down_en = GPIO_PULLDOWN_DISABLE;
    out.intr_type = GPIO_INTR_DISABLE;
    gpio_config(&out);

    rmt_config_t cfg = RMT_DEFAULT_CONFIG_TX((gpio_num_t)gpio, (rmt_channel_t)rmt_channel);
    cfg.clk_div = kRmtClkDiv;
    if (rmt_config(&cfg) != ESP_OK || rmt_driver_install(cfg.channel, 0, 0) != ESP_OK) {
        ESP_LOGW(TAG, "RMT init failed on GPIO%d — LED unavailable", gpio);
        return false;
    }

    s_pixels = pixel_count;
    s_channel = rmt_channel;
    s_ready = true;

    // Blank the strip at boot: a dark LED must never mean "unknown state".
    set_all(kOff);
    ESP_LOGI(TAG, "LED ready: GPIO%d, %d pixel(s), RMT channel %d",
             gpio, pixel_count, rmt_channel);
    return true;
}

void set_all(Rgb color) {
    if (!s_ready) return;

    const int bits = s_pixels * 24;
    for (int i = 0; i < bits; ++i) {
        const int pixel = i / 24;
        const int within = i % 24;
        const int byte = within / 8;
        const int bit = 7 - (within % 8);
        (void)pixel;   // every pixel carries the same colour
        uint8_t val;
        if (byte == 0)      val = (color.g >> bit) & 1u;   // GRB: green first
        else if (byte == 1) val = (color.r >> bit) & 1u;
        else                val = (color.b >> bit) & 1u;

        s_items[i].level0 = 1;
        s_items[i].duration0 = val ? T1H : T0H;
        s_items[i].level1 = 0;
        s_items[i].duration1 = val ? T1L : T0L;
    }
    s_items[bits].level0 = 0;
    s_items[bits].duration0 = 0;
    s_items[bits].level1 = 0;
    s_items[bits].duration1 = RESET_TICKS;

    rmt_write_items((rmt_channel_t)s_channel, s_items, bits + 1, true);
    rmt_wait_tx_done((rmt_channel_t)s_channel, portMAX_DELAY);
}

}  // namespace lma_led
