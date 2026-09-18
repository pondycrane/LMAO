#include "moisture.h"

#include "esp_log.h"
#include "driver/adc.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

// Legacy IDF ADC API, consistent with the project's driver usage (sht30.cpp
// uses driver/i2c.h the same way).  GPIO32 == ADC1 channel 4; 11 dB attenuation
// and 12-bit width match the verified probe readings (air ~2068 / water ~1580).

static const char* TAG = "moisture";
static bool g_inited = false;

bool moisture_init(void) {
    if (g_inited) return true;
    if (adc1_config_width(ADC_WIDTH_BIT_12) != ESP_OK) return false;
    if (adc1_config_channel_atten(ADC1_CHANNEL_4, ADC_ATTEN_DB_11) != ESP_OK) return false;
    g_inited = true;
    ESP_LOGI(TAG, "ADC1_CH4 (GPIO32) ready (12-bit / 11 dB)");
    return true;
}

bool moisture_read_percent(float* out_pct) {
    if (!g_inited && !moisture_init()) return false;
    int raw = adc1_get_raw(ADC1_CHANNEL_4);
    if (raw < 0) {
        ESP_LOGW(TAG, "ADC read failed (raw=%d)", raw);
        return false;
    }
    *out_pct = moisture_percent_from_counts(raw);
    ESP_LOGD(TAG, "raw=%d -> %.1f%%", raw, *out_pct);
    return true;
}
