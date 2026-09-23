#pragma once

#include <stdint.h>

/* Pure DHT20/AHT20 raw->engineering-unit conversion (no ESP-IDF, host-testable).
 * Matches cardputer_client/lib/sensors/dht20.py:
 *   humidity    = (hum_raw  / 1048576.0) * 100.0
 *   temperature = (temp_raw / 1048576.0) * 200.0 - 50.0
 */
namespace dht20_math {

    inline float humidity_pct(uint32_t hum_raw) {
        return ((float)hum_raw / 1048576.0f) * 100.0f;
    }

    inline float temp_celsius(uint32_t temp_raw) {
        return ((float)temp_raw / 1048576.0f) * 200.0f - 50.0f;
    }

}
