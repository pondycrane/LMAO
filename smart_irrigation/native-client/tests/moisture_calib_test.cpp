// Host unit test for the soil-moisture calibration math (pure function,
// no ESP-IDF dependency — exercise the same code the firmware compiles).
#include <cstdio>

#include "moisture.h"

static int failures = 0;

#define CHECK_NEAR(actual, expected, label)                                   \
    do {                                                                      \
        float a = (float)(actual);                                            \
        float e = (float)(expected);                                          \
        if (a < e - 0.01f || a > e + 0.01f) {                                 \
            std::printf("FAIL %s: %.3f != %.3f\n", label, a, e);              \
            failures++;                                                       \
        } else {                                                              \
            std::printf("ok   %s\n", label);                                  \
        }                                                                     \
    } while (0)

#define CHECK_TRUE(cond, label)                                               \
    do {                                                                      \
        if (!(cond)) {                                                        \
            std::printf("FAIL %s\n", label);                                  \
            failures++;                                                       \
        } else {                                                              \
            std::printf("ok   %s\n", label);                                  \
        }                                                                     \
    } while (0)

int main() {
    // Verified calibration end-points (hardware-verification.md 2026-09-16):
    // air ~2068 counts == dry (0 %), submerged ~1580 counts == saturated (100 %).
    CHECK_NEAR(moisture_percent_from_counts(2068), 0.0f, "dry-as-air -> 0%");
    CHECK_NEAR(moisture_percent_from_counts(1580), 100.0f, "submerged -> 100%");
    CHECK_NEAR(moisture_percent_from_counts(1824), 50.0f, "midpoint -> 50%");

    // Clamp outside the calibration span.
    CHECK_NEAR(moisture_percent_from_counts(3000), 0.0f, "above dry clamps to 0%");
    CHECK_NEAR(moisture_percent_from_counts(0), 100.0f, "below wet clamps to 100%");

    // Polarity: lower ADC counts == wetter == higher %.
    CHECK_TRUE(
        moisture_percent_from_counts(1600) > moisture_percent_from_counts(2000),
        "lower counts = wetter");

    if (failures == 0) {
        std::printf("ALL PASS\n");
        return 0;
    }
    std::printf("%d failure(s)\n", failures);
    return 1;
}
