#include "dht20.h"
#include "dht20_math.h"

#include "driver/i2c.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char* TAG = "dht20";
static i2c_port_t g_port = I2C_NUM_0;
static uint8_t g_addr = 0x38;
static bool g_init = false;

namespace dht20 {

    bool init(int sda_pin, int scl_pin, uint8_t addr) {
        i2c_config_t conf = {};
        conf.mode = I2C_MODE_MASTER;
        conf.sda_io_num = sda_pin;
        conf.scl_io_num = scl_pin;
        conf.sda_pullup_en = GPIO_PULLUP_ENABLE;
        conf.scl_pullup_en = GPIO_PULLUP_ENABLE;
        conf.master.clk_speed = 100000;   /* 100 kHz per LMAO I2C convention */
        if (i2c_param_config(g_port, &conf) != ESP_OK) return false;
        if (i2c_driver_install(g_port, I2C_MODE_MASTER, 0, 0, 0) != ESP_OK) return false;
        vTaskDelay(pdMS_TO_TICKS(100));

        // Soft reset (matches dht20.py _init_sensor).
        const uint8_t reset = 0xba;
        i2c_master_write_to_device(g_port, addr, &reset, 1, 100 / portTICK_PERIOD_MS);
        vTaskDelay(pdMS_TO_TICKS(20));

        // Normal measurement mode: check the status byte 0x71; if the
        // calibration bit is unset, run the standard calibration command.
        uint8_t status_cmd[1] = {0x71};
        uint8_t status[1] = {0};
        i2c_master_write_read_device(g_port, addr, status_cmd, 1, status, 1,
                                     100 / portTICK_PERIOD_MS);
        if (!(status[0] & 0x18)) {
            const uint8_t calib[3] = {0xe1, 0x08, 0x00};
            i2c_master_write_to_device(g_port, addr, calib, 3, 100 / portTICK_PERIOD_MS);
        }
        g_addr = addr;
        g_init = true;
        return true;
    }

    bool read(float* temp_c, float* humidity_pct) {
        if (!g_init) return false;
        const uint8_t trig[3] = {0xac, 0x33, 0x00};
        if (i2c_master_write_to_device(g_port, g_addr, trig, 3, 100 / portTICK_PERIOD_MS) != ESP_OK)
            return false;
        vTaskDelay(pdMS_TO_TICKS(80));

        uint8_t data[7] = {0};
        if (i2c_master_read_from_device(g_port, g_addr, data, 7, 200 / portTICK_PERIOD_MS) != ESP_OK)
            return false;
        if (data[0] & 0x80) {
            // Sensor busy / no response.
            return false;
        }

        const uint32_t hum_raw = ((uint32_t)data[1] << 12) | ((uint32_t)data[2] << 4) | (data[3] >> 4);
        const uint32_t temp_raw = (((uint32_t)data[3] & 0x0F) << 16) | ((uint32_t)data[4] << 8) | data[5];

        *humidity_pct = dht20_math::humidity_pct(hum_raw);
        *temp_c = dht20_math::temp_celsius(temp_raw);
        ESP_LOGD(TAG, "temp=%.2f hum=%.2f", *temp_c, *humidity_pct);
        return true;
    }

}
