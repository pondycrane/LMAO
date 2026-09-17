#pragma once

#include <stdint.h>

// ENV III (SHT30) on the DTU base Port A — I2C SCL=G21 / SDA=G25 @ 100kHz.
#define SHT30_SCL 21
#define SHT30_SDA 25
#define SHT30_ADDR 0x44

typedef struct {
    float temp_c;
    float hum_pct;
} Sht30Reading;

// Read air temperature + humidity. Returns true on success.
// Raises nothing on bus error — returns false (try again next cycle).
bool sht30_read(Sht30Reading* out);
