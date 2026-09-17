#include "sht30.h"

#include <string.h>
#include "esp_log.h"
#include "driver/i2c.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char* TAG = "sht30";
static i2c_port_t g_port = I2C_NUM_0;
static bool g_init = false;

bool sht30_read(Sht30Reading* out) {
    if (!g_init) {
        i2c_config_t conf = {};
        conf.mode = I2C_MODE_MASTER;
        conf.sda_io_num = SHT30_SDA;
        conf.scl_io_num = SHT30_SCL;
        conf.sda_pullup_en = GPIO_PULLUP_ENABLE;
        conf.scl_pullup_en = GPIO_PULLUP_ENABLE;
        conf.master.clk_speed = 100000;
        if (i2c_param_config(g_port, &conf) != ESP_OK) return false;
        if (i2c_driver_install(g_port, I2C_MODE_MASTER, 0, 0, 0) != ESP_OK) return false;
        g_init = true;
    }

    // SHT30 high-repeatability, no clock stretch.
    const uint8_t cmd[2] = {0x2c, 0x06};
    if (i2c_master_write_to_device(g_port, SHT30_ADDR, cmd, 2, 100 / portTICK_PERIOD_MS) != ESP_OK)
        return false;
    vTaskDelay(pdMS_TO_TICKS(60));

    uint8_t buf[6] = {0};
    if (i2c_master_read_from_device(g_port, SHT30_ADDR, buf, 6, 200 / portTICK_PERIOD_MS) != ESP_OK)
        return false;

    uint16_t raw_t = ((uint16_t)buf[0] << 8) | buf[1];
    uint16_t raw_h = ((uint16_t)buf[3] << 8) | buf[4];
    out->temp_c = -45.0f + 175.0f * (float)raw_t / 65535.0f;
    out->hum_pct = 100.0f * (float)raw_h / 65535.0f;
    ESP_LOGD(TAG, "temp=%.2f hum=%.2f", out->temp_c, out->hum_pct);
    return true;
}
