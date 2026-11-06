/*
 * RoboDog speaker output bring-up — see robodog_speaker.h.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include "robodog_speaker.h"

#include <stdbool.h>

#include "driver/gpio.h"
#include "esp_board_manager.h"
#include "dev_audio_codec.h"
#include "esp_log.h"

static const char *TAG = "robodog_spk";

esp_codec_dev_handle_t robodog_speaker_dac(void)
{
    void *dev_handle = NULL;
    if (esp_board_manager_get_device_handle("audio_dac", &dev_handle) != ESP_OK ||
        dev_handle == NULL) {
        return NULL;
    }
    return ((dev_audio_codec_handles_t *)dev_handle)->codec_dev;
}

esp_err_t robodog_speaker_assert_output(void)
{
    static bool s_pa_on;
    if (!s_pa_on) {
        const gpio_config_t io = {
            .pin_bit_mask = 1ULL << ROBODOG_SPEAKER_PA_GPIO,
            .mode = GPIO_MODE_OUTPUT,
            .pull_up_en = GPIO_PULLUP_DISABLE,
            .pull_down_en = GPIO_PULLDOWN_DISABLE,
            .intr_type = GPIO_INTR_DISABLE,
        };
        if (gpio_config(&io) == ESP_OK &&
            gpio_set_level(ROBODOG_SPEAKER_PA_GPIO, 1) == ESP_OK) {
            s_pa_on = true;
            ESP_LOGI(TAG, "speaker PA enabled (GPIO%d -> high)",
                     ROBODOG_SPEAKER_PA_GPIO);
        } else {
            ESP_LOGW(TAG, "failed to enable speaker PA on GPIO%d",
                     ROBODOG_SPEAKER_PA_GPIO);
        }
    }
    esp_codec_dev_handle_t dac = robodog_speaker_dac();
    if (dac == NULL) {
        ESP_LOGW(TAG, "no audio_dac codec handle");
        return ESP_ERR_NOT_FOUND;
    }
    (void)esp_codec_dev_set_out_mute(dac, false);
    (void)esp_codec_dev_set_out_vol(dac, 80);
    return s_pa_on ? ESP_OK : ESP_FAIL;
}
