/*
 * Minimal ES8311 audio codec driver for the Cardputer ADV.
 *
 * The ADV routes audio through an ES8311 codec over I2C (SCL=9, SDA=8); the
 * codec is clocked entirely from the I2S master (there is no MCLK pin on the
 * ADV), so the ES8311 derives its internal MCLK from the I2S BCLK
 * ("MCLK=BCLK" mode; REG02=0x18 gives DIG_MCLK = BCLK*8 = 256*fs for 16-bit
 * I2S slots).
 *
 * Register sequence transcribed from M5Stack's M5Unified factory driver
 * (MIT): _speaker_enabled_cb_cardputer_adv + _microphone_enabled_cb_atom_echo.
 * Kept as a small self-contained driver instead of pulling in the whole
 * esp-adf/audio_hal framework.
 */
#pragma once

#include "esp_err.h"
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// ES8311 7-bit I2C address (0x30 >> 1).
#define ES8311_ADDR 0x18

// Power up ADC + DAC (record + playback) and un-mute.
esp_err_t es8311_init(void);

// DAC volume register value (0xBF = 0 dB, 0x00 = -95.5 dB).
esp_err_t es8311_set_dac_volume(uint8_t reg_value);

// Chip/version from the ID registers (0x00 on I2C failure).
uint8_t es8311_chip_id(void);

#ifdef __cplusplus
}
#endif
