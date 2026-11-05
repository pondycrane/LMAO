#pragma once
// SHTC3 temperature/humidity sensor (Waveshare RLCD-4.2 I2C, addr 0x70).
bool shtc3_init(void);
// Read temperature (degC) and relative humidity (%).  Returns false on any
// I2C/CRC failure; outputs are untouched on failure.
bool shtc3_read(float* temp_c, float* rh_pct);
