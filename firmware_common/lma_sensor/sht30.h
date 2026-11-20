#pragma once

#include <stddef.h>
#include <stdint.h>

// SHT30 air temperature + humidity driver for the M5Stack ENV III, shared by
// every LMAO native firmware that reads one (the Sprout's DTU Port A and the
// humidifier Atom Socket's ENV III).  Single canonical copy.
//
// The ENV III also carries a QMP6988 (pressure, 0x70); it is not read here —
// nothing in LMAO's control laws uses pressure.
//
// Wiring is NOT fixed per board: the Atom Socket's HY2.0-4P port carries
// relay-in / 3V3 / GND / button-out (no I2C at all), while the Atom Lite's
// dedicated I2C is G21=SCL / G25=SDA and its Grove port is G26/G32.  ESP32's
// I2C pins are fully routable, so the caller passes candidate (SDA, SCL) pairs
// and the driver keeps the first one where a real SHT30 answers with a valid
// CRC — the boot log names the pair that worked.
namespace lma_sensor {

    struct Reading {
        float temp_c;
        float hum_pct;
    };

    // Probe `pairs` in order on I2C port 0 and install the driver on the first
    // pair that yields a CRC-valid measurement.  Returns the winning index, or
    // -1 when no ENV III answered.  Safe to call again after a failure
    // (sht30_reprobe) — a Grove plug that lost contact can come back.
    int sht30_init(const int (*pairs)[2], size_t n);

    // Re-run the probe over the same candidate list after a failed read.
    bool sht30_reprobe();

    // One high-repeatability measurement, no clock stretching.
    bool sht30_read(Reading* out);

    // Diagnostics for /status and the boot log.
    bool sht30_ok();
    int sht30_sda();
    int sht30_scl();

}  // namespace lma_sensor
