#pragma once

#include <stdbool.h>
#include <stdint.h>

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

// Fixed-point variant for the control engine (AGENTS.md: no float in the
// engine): the same 2-point curve in Q8.8, 0..25600 for 0..100 %.  Integer
// math, so the engine's thresholds compare exactly and the host tests need no
// float tolerance.  Lives here (not in control.h) so the calibration constants
// stay in one place.
inline int32_t moisture_q8_from_counts(int raw) {
    const int32_t span = (int32_t)(MOISTURE_DRY_COUNT - MOISTURE_WET_COUNT);
    if (span <= 0) return 0;
    // (dry - raw) / span * 100 % * 256, rounded half away from zero.
    int32_t num = ((int32_t)MOISTURE_DRY_COUNT - raw) * 100 * 256;
    num += (num >= 0) ? (span / 2) : -(span / 2);
    int32_t q8 = num / span;
    if (q8 < 0) q8 = 0;
    if (q8 > 25600) q8 = 25600;
    return q8;
}

// Configure ADC1_CH4 (GPIO32) at 12-bit / 11 dB.  Idempotent; safe to call
// after the pump-safety first action (G26 is a different pin).
bool moisture_init(void);

// Read the probe as Q8.8 percent.  Returns false on ADC failure.
bool moisture_read_q8(int32_t* out_q8);
