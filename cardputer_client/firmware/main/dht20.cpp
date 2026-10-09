#include "dht20.h"
#include "dht20_math.h"

#include "driver/i2c_master.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char* TAG = "dht20";
// The ES8311 codec owns I2C_NUM_0 (i2c_master) for the voice path; the DHT20
// uses its own bus on I2C_NUM_1.  The I2C driver families must NOT mix in one
// binary either: the legacy driver aborts at boot whenever the new driver is
// linked in, so both use the new driver.
static i2c_master_bus_handle_t g_bus = NULL;
static i2c_master_dev_handle_t g_dev = NULL;
static uint8_t g_addr = 0x38;
static bool g_init = false;

namespace dht20 {

    bool init(int sda_pin, int scl_pin, uint8_t addr) {
        i2c_master_bus_config_t bus_cfg = {};
        bus_cfg.i2c_port = I2C_NUM_1;
        bus_cfg.sda_io_num = (gpio_num_t)sda_pin;
        bus_cfg.scl_io_num = (gpio_num_t)scl_pin;
        bus_cfg.clk_source = I2C_CLK_SRC_DEFAULT;
        bus_cfg.glitch_ignore_cnt = 7;
        bus_cfg.flags.enable_internal_pullup = true;
        if (i2c_new_master_bus(&bus_cfg, &g_bus) != ESP_OK) return false;
        i2c_device_config_t dev_cfg = {
            .dev_addr_length = I2C_ADDR_BIT_LEN_7,
            .device_address = addr,
            .scl_speed_hz = 100000,   /* 100 kHz per LMAO I2C convention */
        };
        if (i2c_master_bus_add_device(g_bus, &dev_cfg, &g_dev) != ESP_OK) return false;
        vTaskDelay(pdMS_TO_TICKS(100));

        // Soft reset (matches dht20.py _init_sensor).
        const uint8_t reset = 0xba;
        i2c_master_transmit(g_dev, &reset, 1, 100);
        vTaskDelay(pdMS_TO_TICKS(20));

        // Normal measurement mode: check the status byte 0x71; if the
        // calibration bit is unset, run the standard calibration command.
        uint8_t status_cmd[1] = {0x71};
        uint8_t status[1] = {0};
        i2c_master_transmit_receive(g_dev, status_cmd, 1, status, 1, 100);
        if (!(status[0] & 0x18)) {
            const uint8_t calib[3] = {0xe1, 0x08, 0x00};
            i2c_master_transmit(g_dev, calib, 3, 100);
        }
        g_addr = addr;
        g_init = true;
        return true;
    }

    bool read(float* temp_c, float* humidity_pct) {
        if (!g_init) return false;
        const uint8_t trig[3] = {0xac, 0x33, 0x00};
        if (i2c_master_transmit(g_dev, trig, 3, 100) != ESP_OK)
            return false;
        vTaskDelay(pdMS_TO_TICKS(80));

        uint8_t data[7] = {0};
        if (i2c_master_receive(g_dev, data, 7, 100) != ESP_OK)
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
