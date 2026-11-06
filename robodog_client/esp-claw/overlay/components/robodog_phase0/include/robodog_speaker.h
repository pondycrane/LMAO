/*
 * RoboDog speaker output bring-up (LMAO-owned, single copy).
 *
 * The board-manager peripheral init leaves the amplifier pin at the struct
 * default LOW and the ES8311 DAC muted, so a reply is silent until all three
 * are asserted: PA high (GPIO46, active_level=1 in the board YAML), DAC
 * unmuted, volume set.  Phase 2 learned this the hard way (an audible reply
 * only appeared once `voice_speak` asserted all three), so the same three
 * calls live here and both the Phase 2 TTS path and the Phase 3 omni realtime
 * path use them — never a second copy.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once

#include "esp_err.h"
#include "esp_codec_dev.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief The board's `audio_dac` codec handle (ES8311), or NULL when the
 *        board manager has not brought it up.
 */
esp_codec_dev_handle_t robodog_speaker_dac(void);

/**
 * @brief Raise the speaker PA (idempotent) and unmute + set volume on the DAC.
 *        Safe to call on every playback; the PA is configured once.
 * @return ESP_OK when the PA is high and the DAC accepted the unmute.
 */
esp_err_t robodog_speaker_assert_output(void);

/** @brief Speaker PA GPIO (board schematic: GPIO46, active high). */
#define ROBODOG_SPEAKER_PA_GPIO 46

#ifdef __cplusplus
}
#endif
