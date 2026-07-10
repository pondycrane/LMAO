# MicroPython I2C sensor dispatch for Cardputer Grove port.
# Grove port pins on Cardputer ADV: SDA=GPIO21, SCL=GPIO22.

_SDA_PIN = 21
_SCL_PIN = 22


def read_humidity_temperature(
    sensor_type, i2c_addr=0x38, sda_pin=_SDA_PIN, scl_pin=_SCL_PIN
):
    """Read temperature (Celsius) and humidity (%) from the external Grove sensor.

    Args:
        sensor_type: Supported sensor type string (e.g., "DHT20") or None.
        i2c_addr: I2C address of the sensor (default 0x38 for DHT20/AHT20).
        sda_pin: GPIO pin for SDA (default GPIO21).
        scl_pin: GPIO pin for SCL (default GPIO22).

    Returns:
        (temp_celsius, humidity_pct) or (None, None) on failure.
    """
    if sensor_type is None:
        return None, None

    # Lazy import — fails gracefully if I2C or driver not available
    try:
        from machine import SoftI2C, Pin

        i2c = SoftI2C(sda=Pin(sda_pin), scl=Pin(scl_pin))

        if sensor_type == "DHT20":
            from lib.sensors.dht20 import DHT20

            sensor = DHT20(i2c, i2c_addr)
            return sensor.read()

    except ImportError:
        # Missing driver module — let this propagate so the developer
        # discovers the deployment problem immediately rather than
        # silently falling back to die-temp-only mode.
        raise
    except (OSError, ValueError) as e:
        print(f"Sensor read failed: {e}")

    return None, None
