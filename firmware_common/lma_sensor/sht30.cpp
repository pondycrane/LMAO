// SHT30 (M5Stack ENV III) driver — see sht30.h.  Single canonical copy for
// the Sprout and the humidifier Atom Socket.
#include "sht30.h"

#include "driver/i2c.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "sht30_codec.h"

static const char* TAG = "sht30";

namespace {

constexpr uint8_t kAddr = 0x44;
constexpr size_t kMaxPairs = 4;
constexpr i2c_port_t kPort = I2C_NUM_0;

int g_pairs[kMaxPairs][2];
size_t g_pair_count = 0;
int g_pair_idx = -1;
bool g_installed = false;

void bus_down() {
    if (g_installed) {
        i2c_driver_delete(kPort);
        g_installed = false;
    }
}

bool bus_up(int sda, int scl) {
    i2c_config_t conf = {};
    conf.mode = I2C_MODE_MASTER;
    conf.sda_io_num = sda;
    conf.scl_io_num = scl;
    conf.sda_pullup_en = GPIO_PULLUP_ENABLE;
    conf.scl_pullup_en = GPIO_PULLUP_ENABLE;
    conf.master.clk_speed = 100000;
    if (i2c_param_config(kPort, &conf) != ESP_OK) return false;
    if (i2c_driver_install(kPort, I2C_MODE_MASTER, 0, 0, 0) != ESP_OK) return false;
    g_installed = true;
    return true;
}

}  // namespace

namespace lma_sensor {

bool sht30_read(Reading* out) {
    if (!g_installed) return false;

    // High repeatability, clock stretching disabled (0x2C 0x06).
    const uint8_t cmd[2] = {0x2C, 0x06};
    if (i2c_master_write_to_device(kPort, kAddr, cmd, sizeof(cmd),
                                   100 / portTICK_PERIOD_MS) != ESP_OK) {
        return false;
    }
    vTaskDelay(pdMS_TO_TICKS(20));  // 15 ms max for high repeatability

    uint8_t frame[6] = {};
    if (i2c_master_read_from_device(kPort, kAddr, frame, sizeof(frame),
                                    200 / portTICK_PERIOD_MS) != ESP_OK) {
        return false;
    }

    Reading r;
    if (!sht30_decode(frame, &r.temp_c, &r.hum_pct)) {
        ESP_LOGW(TAG, "CRC mismatch (%02x %02x %02x / %02x %02x %02x)",
                 frame[0], frame[1], frame[2], frame[3], frame[4], frame[5]);
        return false;
    }
    *out = r;
    ESP_LOGD(TAG, "temp=%.2f hum=%.2f", r.temp_c, r.hum_pct);
    return true;
}

int sht30_init(const int (*pairs)[2], size_t n) {
    bus_down();
    g_pair_count = n < kMaxPairs ? n : kMaxPairs;
    for (size_t i = 0; i < g_pair_count; i++) {
        g_pairs[i][0] = pairs[i][0];
        g_pairs[i][1] = pairs[i][1];
    }
    g_pair_idx = -1;

    for (size_t i = 0; i < g_pair_count; i++) {
        const int sda = g_pairs[i][0];
        const int scl = g_pairs[i][1];
        if (!bus_up(sda, scl)) {
            ESP_LOGW(TAG, "I2C install failed on SDA=%d SCL=%d", sda, scl);
            continue;
        }
        Reading r;
        if (sht30_read(&r)) {
            g_pair_idx = (int)i;
            ESP_LOGI(TAG, "ENV III found on SDA=%d SCL=%d (%.2f C / %.2f %%RH)",
                     sda, scl, r.temp_c, r.hum_pct);
            return g_pair_idx;
        }
        bus_down();
    }
    ESP_LOGW(TAG, "no ENV III answered on any candidate pair "
                  "(%u tried) — humidity control will fail safe",
             (unsigned)g_pair_count);
    return -1;
}

bool sht30_reprobe() {
    const size_t n = g_pair_count;
    int copy[kMaxPairs][2];
    for (size_t i = 0; i < n; i++) {
        copy[i][0] = g_pairs[i][0];
        copy[i][1] = g_pairs[i][1];
    }
    return sht30_init(copy, n) >= 0;
}

bool sht30_ok() { return g_installed && g_pair_idx >= 0; }

int sht30_sda() { return g_pair_idx >= 0 ? g_pairs[g_pair_idx][0] : -1; }

int sht30_scl() { return g_pair_idx >= 0 ? g_pairs[g_pair_idx][1] : -1; }

}  // namespace lma_sensor
