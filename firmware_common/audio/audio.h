/*
 * I2S audio master for the Cardputer ADV ES8311 codec.
 *
 * ADV wiring: BCLK=41, WS=43, DOUT=42 (to ES8311 DAC/speaker), DIN=46 (from
 * ES8311 ADC/mic).  The ES8311 has no MCLK pin, so it derives its internal
 * MCLK from the I2S BCLK; 16-bit stereo slots give BCLK = 32*fs and
 * DIG_MCLK = BCLK*8 = 256*fs (see es8311.c).  Mono samples ride the left slot.
 */
#pragma once

#include "esp_err.h"
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define AUDIO_SAMPLE_RATE 8000   // codec2 700C frame = 320 samples (40 ms)

// Start the full-duplex I2S master (BCLK=41, WS=43, DOUT=42, DIN=46).
esp_err_t audio_init(void);

// Record n mono 16-bit samples (blocks).  Returns ESP_OK on success.
esp_err_t audio_record(int16_t *out, size_t n);

// Play n mono 16-bit samples (blocks).  Returns ESP_OK on success.
esp_err_t audio_play(const int16_t *in, size_t n);

// RMS (0..32768) of an n-sample mono buffer — mic/speaker level check.
float audio_rms(const int16_t *samples, size_t n);

#ifdef __cplusplus
}
#endif
