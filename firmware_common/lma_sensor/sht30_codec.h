#pragma once

#include <stdint.h>

// Pure SHT30 helpers, no I/O and no ESP-IDF headers, so the host tests can
// exercise them (//firmware_common:sht30_codec_test).
//
// The SHT30 answers a measurement with 6 bytes: two data bytes + one CRC byte
// per value.  The CRC is the Sensirion CRC-8 (poly 0x31, init 0xFF, no
// reflection, no final XOR) over the two data bytes.  It is NOT optional in
// practice: with no sensor on the bus, a pulled-up SDA reads 0xFF and decodes
// to a plausible-looking 130 C / 100 % RH, so without the check a missing ENV
// III would report a full, hot room and the law would humidify against it.
namespace lma_sensor {

    inline uint8_t sht30_crc8(const uint8_t* data, uint8_t len) {
        uint8_t crc = 0xFF;
        for (uint8_t i = 0; i < len; i++) {
            crc ^= data[i];
            for (uint8_t b = 0; b < 8; b++) {
                crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x31)
                                   : (uint8_t)(crc << 1);
            }
        }
        return crc;
    }

    // SHT30 datasheet linearisation: T = -45 + 175 * raw / 65535,
    // RH = 100 * raw / 65535.
    inline float sht30_temp_c(uint16_t raw) {
        return -45.0f + 175.0f * (float)raw / 65535.0f;
    }

    inline float sht30_hum_pct(uint16_t raw) {
        return 100.0f * (float)raw / 65535.0f;
    }

    // Decode a 6-byte measurement frame.  False when either CRC fails.
    inline bool sht30_decode(const uint8_t* frame, float* temp_c, float* hum_pct) {
        if (sht30_crc8(frame, 2) != frame[2]) return false;
        if (sht30_crc8(frame + 3, 2) != frame[5]) return false;
        const uint16_t raw_t = (uint16_t)((frame[0] << 8) | frame[1]);
        const uint16_t raw_h = (uint16_t)((frame[3] << 8) | frame[4]);
        *temp_c = sht30_temp_c(raw_t);
        *hum_pct = sht30_hum_pct(raw_h);
        return true;
    }

}  // namespace lma_sensor
