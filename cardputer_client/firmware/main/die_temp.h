#pragma once

// ESP32-S3 internal die-temperature sensor (TSENS), the native equivalent of
// MicroPython's esp32.mcu_temperature().  sensor_id 1 "C".
namespace die_temp {

    // One-shot install+enable.  Returns true on success; safe to call once.
    bool init();

    // Returns degrees Celsius, or NaN on failure.
    float read_celsius();

}
