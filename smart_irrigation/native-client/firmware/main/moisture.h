#pragma once

#include <stdbool.h>

// Soil-moisture (M5Stack Watering Unit U101 capacitive probe) on GPIO32,
// which is ADC1 channel 4.  Values are the verified 12-bit / 11 dB readings
// from hardware-verification.md (2026-09-16): air ~2068, submerged ~1580.
// Lower counts == wetter, so we normalise to moisture % with 100 % ==
// saturated (as submerged) and 0 % == dry-as-air.
#define MOISTURE_DRY_COUNT 2068.0f
#define MOISTURE_WET_COUNT 1580.0f

// Pure calibration math (no ESP-IDF dependency — host-testable).
// 0..100 %; clamps outside the dry/wet calibration span.
inline float moisture_percent_from_counts(int raw) {
    const float span = MOISTURE_DRY_COUNT - MOISTURE_WET_COUNT;  // 488 counts
    if (span <= 0.0f) return 0.0f;
    float pct = (MOISTURE_DRY_COUNT - (float)raw) / span * 100.0f;
    if (pct < 0.0f) pct = 0.0f;
    if (pct > 100.0f) pct = 100.0f;
    return pct;
}

// Configure ADC1_CH4 (GPIO32) at 12-bit / 11 dB.  Idempotent; safe to call
// after the pump-safety first action (G26 is a different pin).
bool moisture_init(void);

// Read the probe and normalise to 0..100 %.  Returns false on ADC failure.
bool moisture_read_percent(float* out_pct);
