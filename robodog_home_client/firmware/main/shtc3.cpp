// SHTC3 temperature/humidity driver for the Waveshare RLCD-4.2 board.
//
// SHTC3 command set (datasheet):
//   0x3517 wake-up, 0xB098 sleep
//   0x7866 measure T first, 0x58E5 measure RH first
// Each measurement returns 6 bytes: 2-byte MSB-first value + CRC-8
// (poly 0x31, init 0xFF) for temperature, then the same for humidity.
//
// Conversion: T = -45 + 175 * raw / 65536; RH = 100 * raw / 65536.
#include "shtc3.h"

#include <cstring>

#include "driver/i2c.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "config.h"

static const char* TAG = "shtc3";
static bool s_ready = false;

namespace {

uint8_t crc8(const uint8_t* data, size_t len) {
    uint8_t crc = 0xFF;
    for (size_t i = 0; i < len; ++i) {
        crc ^= data[i];
        for (int b = 0; b < 8; ++b) {
            crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x31)
                               : (uint8_t)(crc << 1);
        }
    }
    return crc;
}

bool write_cmd(uint8_t msb, uint8_t lsb) {
    const uint8_t cmd[2] = {msb, lsb};
    return i2c_master_write_to_device(RH_I2C_PORT, RH_SHTC3_ADDR, cmd,
                                      sizeof(cmd),
                                      pdMS_TO_TICKS(100)) == ESP_OK;
}

// Read one measurement word (2 data bytes + CRC) at *word*.
bool read_word(uint16_t* word) {
    uint8_t buf[3] = {0};
    if (i2c_master_read_from_device(RH_I2C_PORT, RH_SHTC3_ADDR, buf,
                                    sizeof(buf),
                                    pdMS_TO_TICKS(100)) != ESP_OK) {
        return false;
    }
    if (crc8(buf, 2) != buf[2]) {
        ESP_LOGW(TAG, "CRC mismatch (got 0x%02x want 0x%02x)", buf[2],
                 crc8(buf, 2));
        return false;
    }
    *word = (uint16_t)((buf[0] << 8) | buf[1]);
    return true;
}

}  // namespace

bool shtc3_init(void) {
    i2c_config_t cfg = {};
    cfg.mode = I2C_MODE_MASTER;
    cfg.sda_io_num = RH_I2C_SDA_PIN;
    cfg.scl_io_num = RH_I2C_SCL_PIN;
    cfg.sda_pullup_en = GPIO_PULLUP_ENABLE;
    cfg.scl_pullup_en = GPIO_PULLUP_ENABLE;
    cfg.master.clk_speed = RH_I2C_FREQ_HZ;
    cfg.clk_flags = 0;

    esp_err_t err = i2c_param_config(RH_I2C_PORT, &cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "i2c_param_config failed: %s", esp_err_to_name(err));
        return false;
    }
    err = i2c_driver_install(RH_I2C_PORT, I2C_MODE_MASTER, 0, 0, 0);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ESP_LOGE(TAG, "i2c_driver_install failed: %s", esp_err_to_name(err));
        return false;
    }
    s_ready = true;
    ESP_LOGI(TAG, "SHTC3 on SDA=%d SCL=%d addr=0x%02x",
             (int)RH_I2C_SDA_PIN, (int)RH_I2C_SCL_PIN, RH_SHTC3_ADDR);
    return true;
}

bool shtc3_read(float* temp_c, float* rh_pct) {
    if (!s_ready || temp_c == nullptr || rh_pct == nullptr) return false;

    if (!write_cmd(0x35, 0x17)) {  // wake up
        ESP_LOGW(TAG, "wake failed");
        return false;
    }
    vTaskDelay(pdMS_TO_TICKS(1));

    // One measurement returns T then RH, each as 2 data bytes + CRC.  Issuing
    // a second "measure RH first" command (0x58E5) instead leaves the sensor
    // mid-conversion and the second word read fails, so read both words from
    // this single trigger.
    if (!write_cmd(0x78, 0x66)) {  // measure T first
        ESP_LOGW(TAG, "measure failed");
        return false;
    }
    vTaskDelay(pdMS_TO_TICKS(13));

    uint16_t raw_t = 0;
    if (!read_word(&raw_t)) return false;
    uint16_t raw_rh = 0;
    if (!read_word(&raw_rh)) return false;

    (void)write_cmd(0xB0, 0x98);  // sleep

    *temp_c = -45.0f + 175.0f * (float)raw_t / 65536.0f;
    *rh_pct = 100.0f * (float)raw_rh / 65536.0f;
    return true;
}
