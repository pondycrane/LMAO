#include "pump.h"

#include "esp_log.h"
#include "esp_timer.h"
#include "driver/gpio.h"

static const char* TAG = "pump";

static bool     g_inited = false;
static bool     g_on = false;
static bool     g_refused_logged = false;
static uint32_t g_last_on_ms = 0;       // esp_timer ms when the pin went ON
static uint32_t g_on_ms_total = 0;      // since boot
static uint32_t g_interval_ms = 0;      // since pump_take_interval_ms()

bool pump_init(void) {
    gpio_config_t io = {};
    io.pin_bit_mask = (1ULL << PUMP_GPIO);
    io.mode = GPIO_MODE_OUTPUT;
    // Internal pull-down is a belt only: it keeps the pad defined once the
    // output driver is off (reset/deep sleep), but ~45 kOhm is far weaker than
    // the required 10 kOhm hardware pull-down (issue #119) and covers nothing
    // before gpio_config runs.
    io.pull_up_en = GPIO_PULLUP_DISABLE;
    io.pull_down_en = GPIO_PULLDOWN_ENABLE;
    io.intr_type = GPIO_INTR_DISABLE;
    if (gpio_config(&io) != ESP_OK) {
        ESP_LOGE(TAG, "gpio_config(G%d) failed", PUMP_GPIO);
        return false;
    }
    gpio_set_level((gpio_num_t)PUMP_GPIO, 0);
    g_on = false;
    g_inited = true;
    ESP_LOGI(TAG, "pump enable G%d driven LOW (OFF), actuation %s", PUMP_GPIO,
             PUMP_ACTUATION_ENABLED ? "ENABLED" : "DISABLED (dry run)");
    return true;
}

void pump_set(bool on) {
    if (!g_inited && !pump_init()) return;
    if (on && !PUMP_ACTUATION_ENABLED) {
        if (!g_refused_logged) {
            ESP_LOGW(TAG,
                     "refusing to energise G%d: PUMP_ACTUATION_ENABLED=0 "
                     "(#119 hardware pull-down not verified)",
                     PUMP_GPIO);
            g_refused_logged = true;
        }
        return;
    }
    if (on == g_on) return;
    const uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
    const bool was_on = g_on;
    g_on = on;
    gpio_set_level((gpio_num_t)PUMP_GPIO, on ? 1 : 0);
    if (was_on && !on) {
        const uint32_t width = now - g_last_on_ms;
        g_on_ms_total += width;
        g_interval_ms += width;
    }
    if (on) g_last_on_ms = now;
    ESP_LOGI(TAG, "pump %s (boot total %ums)", on ? "ON" : "OFF",
             (unsigned)g_on_ms_total);
}

bool pump_is_on(void) { return g_on; }

uint32_t pump_on_ms_total(void) { return g_on_ms_total; }

uint32_t pump_take_interval_ms(void) {
    const uint32_t v = g_interval_ms;
    g_interval_ms = 0;
    return v;
}
