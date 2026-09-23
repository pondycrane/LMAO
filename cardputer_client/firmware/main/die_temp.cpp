#include "die_temp.h"

#include <math.h>
#include "esp_log.h"
#include "driver/temperature_sensor.h"

static const char* TAG = "die_temp";
static temperature_sensor_handle_t g_tsens = nullptr;

namespace die_temp {

    bool init() {
        if (g_tsens) return true;
        /* Internal TSENS calibrated range. ESP32-S3 calibrated windows in
         * ESP-IDF v5.3.1 are [-40,20], [-30,50], [-10,80], [20,100], [50,125]
         * (temperature_sensor_periph.c). A -20..80 request matches none and
         * install fails — use -10..80 (error +-1C), the tightest window that
         * covers ambient and die temps. */
        const temperature_sensor_config_t cfg = TEMPERATURE_SENSOR_CONFIG_DEFAULT(-10, 80);
        if (temperature_sensor_install(&cfg, &g_tsens) != ESP_OK) {
            ESP_LOGW(TAG, "temperature_sensor_install failed");
            g_tsens = nullptr;
            return false;
        }
        if (temperature_sensor_enable(g_tsens) != ESP_OK) {
            ESP_LOGW(TAG, "temperature_sensor_enable failed");
            temperature_sensor_uninstall(g_tsens);
            g_tsens = nullptr;
            return false;
        }
        return true;
    }

    float read_celsius() {
        if (!g_tsens) return NAN;
        float c = NAN;
        if (temperature_sensor_get_celsius(g_tsens, &c) != ESP_OK) {
            return NAN;
        }
        return c;
    }

}
