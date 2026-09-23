#pragma once

#include <stdbool.h>
#include <stdint.h>

// DHT20 / AHT20 Grove temperature+humidity I2C sensor (addr 0x38) on the
// Cardputer ADV Grove port.  Native port of cardputer_client/lib/sensors/dht20.py.
// Only the humidity is reported upstream (sensor_id 2 "%"); the on-sensor
// temperature is read but discarded — the LMAO contract keeps sensor_id 1 as
// the ESP32 die temperature even when a DHT20 is attached.
namespace dht20 {

    // One-shot I2C init. sda/scI GPIO numbers; addr defaults 0x38.
    bool init(int sda_pin, int scl_pin, uint8_t addr = 0x38);

    // Reads the sensor. *humidity_pct in [0,100]; *temp_c in Celsius.
    // Returns false if the sensor is absent/busy (semantics match the
    // MicroPython driver: never throws, callers treat false as "no reading").
    bool read(float* temp_c, float* humidity_pct);

}
