# DHT20 / AHT20 I2C sensor driver for MicroPython.
# Minimal implementation — only init + read, no CRC check for footprint.
import time


class DHT20:
    """DHT20 (AHT20-based) I2C temperature and humidity sensor driver.

    Args:
        i2c: Machine SoftI2C instance (pre-initialised with correct pins).
        addr: I2C address (default 0x38, factory default).
    """

    def __init__(self, i2c, addr=0x38):
        self.i2c = i2c
        self.addr = addr
        self._init_sensor()

    def _init_sensor(self):
        """Initialise the AHT20 chip: soft reset, then set normal measurement mode."""
        time.sleep_ms(100)
        # Soft reset
        self.i2c.writeto(self.addr, b"\xba")
        time.sleep_ms(20)
        # Set to normal measurement mode (no calibration here — sensor self-calibrates)
        buf = bytearray(3)
        self.i2c.readfrom_mem_into(self.addr, 0x71, buf)
        if not (buf[0] & 0x18):
            # Standard AHT20 calibration command per datasheet
            self.i2c.writeto(self.addr, b"\xe1\x08\x00")

    def read(self):
        """Trigger a measurement and return (temperature_celsius, humidity_pct).

        Returns:
            (temp, humidity) as floats, or (None, None) if the sensor is busy
            or returns no data.
        """
        self.i2c.writeto(self.addr, b"\xac\x33\x00")
        time.sleep_ms(80)
        data = self.i2c.readfrom(self.addr, 7)

        if not data or (data[0] & 0x80):
            # Sensor busy or no response
            return None, None

        hum_raw = (data[1] << 12) | (data[2] << 4) | (data[3] >> 4)
        temp_raw = ((data[3] & 0x0F) << 16) | (data[4] << 8) | data[5]

        humidity = (hum_raw / 1048576.0) * 100.0
        temperature = (temp_raw / 1048576.0) * 200.0 - 50.0

        return temperature, humidity
