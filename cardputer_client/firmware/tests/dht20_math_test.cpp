// Host unit test for the DHT20 raw→C/% conversion math.
//
// The conversion lives inline in dht20_math.h (pure, no ESP-IDF) so the host
// can exercise exactly the formulas the device firmware compiles.  Golden
// values computed from the MicroPython driver (cardputer_client/lib/sensors/
// dht20.py): humidity = (hum_raw / 1048576.0) * 100.0,
// temperature = (temp_raw / 1048576.0) * 200.0 - 50.0.
#include <cstdio>
#include <cmath>

#include "dht20_math.h"

static int failures = 0;

#define CHECK_NEAR(actual, expected, tol, label)                             \
    do {                                                                     \
        double a = (double)(actual);                                         \
        double e = (double)(expected);                                       \
        if (std::fabs(a - e) > (tol)) {                                      \
            std::printf("FAIL %s\n  actual  : %.6f\n  expected: %.6f\n",     \
                        label, a, e);                                        \
            failures++;                                                      \
        } else {                                                             \
            std::printf("ok   %s\n", label);                                 \
        }                                                                    \
    } while (0)

int main() {
    // 50.0% humidity: hum_raw = 0.50 * 1048576 = 524288
    CHECK_NEAR(dht20_math::humidity_pct(524288u), 50.0f, 1e-4, "humidity 50%");

    // 0% -> 0; 100% -> 1048576
    CHECK_NEAR(dht20_math::humidity_pct(0u), 0.0f, 1e-4, "humidity 0%");
    CHECK_NEAR(dht20_math::humidity_pct(1048576u), 100.0f, 1e-4, "humidity 100%");

    // 25.0 C: temp_raw = (25.0 + 50.0) / 200.0 * 1048576 = 393216
    CHECK_NEAR(dht20_math::temp_celsius(393216u), 25.0f, 1e-4, "temp 25C");

    // 0 C -> 262144 ; 80 C -> 681574.4
    CHECK_NEAR(dht20_math::temp_celsius(262144u), 0.0f, 1e-4, "temp 0C");
    CHECK_NEAR(dht20_math::temp_celsius(681574u), 80.0f, 1e-2, "temp 80C");

    if (failures == 0) {
        std::printf("ALL PASS\n");
        return 0;
    }
    std::printf("%d failure(s)\n", failures);
    return 1;
}
