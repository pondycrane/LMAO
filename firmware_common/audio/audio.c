#include "audio.h"

#include "driver/i2s_std.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <math.h>
#include <stdlib.h>
#include <string.h>

// Bounded waits: if the ES8311 ADC is silent (clock mismatch) we must fail
// fast, not block the codec task forever.
#define I2S_RXTX_TIMEOUT pdMS_TO_TICKS(200)

#define TAG "audio"

static i2s_chan_handle_t g_tx = NULL;
static i2s_chan_handle_t g_rx = NULL;

// Stereo 16-bit slots: BCLK = WS-period * 2 * 16 bits = 32*fs, which the
// ES8311 holds at DIG_MCLK = BCLK*8 = 256*fs (M5Unified ADV config, REG02=0x18).
#define BYTES_PER_SAMPLE 4U

esp_err_t audio_init(void) {
    i2s_chan_config_t ccfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0,
                                                        I2S_ROLE_MASTER);
    ccfg.auto_clear = true;   // zero-fill RX instead of returning stale frames
    esp_err_t rc = i2s_new_channel(&ccfg, &g_tx, &g_rx);
    if (rc != ESP_OK) {
        ESP_LOGE(TAG, "i2s_new_channel: %d", rc);
        return rc;
    }

    i2s_std_config_t cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(AUDIO_SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(
            I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
    };
    cfg.gpio_cfg.mclk = I2S_GPIO_UNUSED;
    cfg.gpio_cfg.bclk = 41;
    cfg.gpio_cfg.ws = 43;
    cfg.gpio_cfg.dout = 42;
    cfg.gpio_cfg.din = 46;
    // 16-bit slots (NOT 32): BCLK = 32*fs -> ES8311 DIG_MCLK = BCLK*8 = 256*fs.
    // A 32-bit slot width would double BCLK and the codec PLL would never lock.

    rc = i2s_channel_init_std_mode(g_tx, &cfg);
    if (rc != ESP_OK) {
        ESP_LOGE(TAG, "tx init: %d", rc);
        return rc;
    }
    rc = i2s_channel_init_std_mode(g_rx, &cfg);
    if (rc != ESP_OK) {
        ESP_LOGE(TAG, "rx init: %d", rc);
        return rc;
    }
    if ((rc = i2s_channel_enable(g_tx)) != ESP_OK ||
        (rc = i2s_channel_enable(g_rx)) != ESP_OK) {
        ESP_LOGE(TAG, "enable: %d", rc);
        return rc;
    }
    ESP_LOGI(TAG, "I2S master up: 8k/16bit stereo slots, bclk=41 ws=43 "
                  "dout=42 din=46");
    return ESP_OK;
}

esp_err_t audio_record(int16_t *out, size_t n) {
    // n mono samples = n 32-bit slots (one per sample) = n*BYTES_PER_SAMPLE.
    void *buf = malloc(n * BYTES_PER_SAMPLE);
    if (!buf) {
        return ESP_ERR_NO_MEM;
    }
    size_t read = 0;
    esp_err_t rc = i2s_channel_read(g_rx, buf, n * BYTES_PER_SAMPLE, &read,
                                    I2S_RXTX_TIMEOUT);
    if (rc == ESP_OK) {
        read = read < n * BYTES_PER_SAMPLE ? read : n * BYTES_PER_SAMPLE;
        const uint8_t *b = (const uint8_t *)buf;
        size_t ns = 0;
        for (size_t i = 0; i + 3 < read; i += 4) {
            // Stereo 16-bit: take the left slot of each frame.
            out[i / 4] = (int16_t)(b[i] | (b[i + 1] << 8));
            ns++;
        }
        for (; ns < n; ns++) {
            out[ns] = 0;   // partial read -> zero the tail, never garbage
        }
    }
    free(buf);
    return rc;
}

esp_err_t audio_play(const int16_t *in, size_t n) {
    void *buf = malloc(n * BYTES_PER_SAMPLE);
    if (!buf) {
        return ESP_ERR_NO_MEM;
    }
    uint8_t *b = (uint8_t *)buf;
    for (size_t i = 0; i < n; i++) {
        // Stereo 16-bit: left slot = sample, right slot = silence.
        b[i * 4 + 0] = (uint8_t)(in[i] & 0xff);
        b[i * 4 + 1] = (uint8_t)((in[i] >> 8) & 0xff);
        b[i * 4 + 2] = 0;
        b[i * 4 + 3] = 0;
    }
    size_t written = 0;
    esp_err_t rc = i2s_channel_write(g_tx, buf, n * BYTES_PER_SAMPLE, &written,
                                     I2S_RXTX_TIMEOUT);
    free(buf);
    return rc;
}

float audio_rms(const int16_t *samples, size_t n) {
    double acc = 0.0;
    for (size_t i = 0; i < n; i++) {
        const double v = (double)samples[i];
        acc += v * v;
    }
    return n ? (float)sqrt(acc / n) : 0.0f;
}
