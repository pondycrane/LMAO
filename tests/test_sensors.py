"""Unit tests for cardputer_client.lib.sensors — DHT20 driver and dispatch.

Tests the DHT20/AHT20 I2C sensor driver's bit-manipulation math and I2C
init sequence, plus the read_humidity_temperature() dispatch function.

Run with::

    bazel test //tests:test_sensors --test_output=all
"""

import sys
import time as _real_time
from unittest.mock import MagicMock, patch, call
import pytest


# ── DHT20 driver tests ──────────────────────────────────────────────
# These tests mock machine.SoftI2C and verify the data sheet formulas.


class TestDHT20Driver:
    """Unit tests for the DHT20 I2C sensor driver."""

    @pytest.fixture(autouse=True)
    def setup_mocks(self):
        """Mock machine.SoftI2C and time.sleep_ms so the driver can be imported on CPython."""
        self.mock_soft_i2c = MagicMock()
        sys.modules["machine"] = MagicMock()
        sys.modules["machine"].SoftI2C = MagicMock(return_value=self.mock_soft_i2c)
        sys.modules["machine"].Pin = MagicMock()

        # MicroPython time.sleep_ms doesn't exist on CPython — monkey-patch it
        import time
        if not hasattr(time, "sleep_ms"):
            time.sleep_ms = lambda ms: _real_time.sleep(ms / 1000.0)

        # Import the driver (now that machine is mocked)
        from cardputer_client.lib.sensors.dht20 import DHT20

        self.DHT20 = DHT20
        yield
        # Cleanup
        if "cardputer_client.lib.sensors.dht20" in sys.modules:
            del sys.modules["cardputer_client.lib.sensors.dht20"]
        if hasattr(time, "sleep_ms"):
            delattr(time, "sleep_ms")

    # ── Constructor tests ────────────────────────────────────────
    def test_constructor_sends_soft_reset(self):
        """Constructor should send soft-reset command (0xBA)."""
        sensor = self.DHT20(self.mock_soft_i2c)

        # Check that writeto was called with 0xBA at some point (soft reset)
        reset_calls = [
            c
            for c in self.mock_soft_i2c.writeto.call_args_list
            if c[0][1] == b"\xba"
        ]
        assert len(reset_calls) >= 1, (
            "Expected soft reset command (0xBA) during init"
        )

    def test_constructor_uses_custom_address(self):
        """Constructor should use the provided I2C address."""
        sensor = self.DHT20(self.mock_soft_i2c, addr=0x39)

        # The sensor was initialized with the custom address
        assert sensor.addr == 0x39

    def test_constructor_default_address_is_0x38(self):
        """Default I2C address should be 0x38 (factory default)."""
        sensor = self.DHT20(self.mock_soft_i2c)
        assert sensor.addr == 0x38

    def test_constructor_sends_calibration_command(self):
        """Constructor should send AHT20 calibration command (0xE1 0x08 0x00)
        when sensor status register shows calibration needed."""
        # Simulate the readfrom_mem_into callback to fill buf with 0x07
        # (0x07 & 0x18 == 0x00, so calibration path is triggered)
        def fill_cal_needed(addr, reg, buf):
            buf[0] = 0x07
            buf[1] = 0x00
            buf[2] = 0x00
        self.mock_soft_i2c.readfrom_mem_into.side_effect = fill_cal_needed

        self.mock_soft_i2c.reset_mock()
        sensor = self.DHT20(self.mock_soft_i2c)

        cal_calls = [
            c
            for c in self.mock_soft_i2c.writeto.call_args_list
            if c[0][1] == b"\xe1\x08\x00"
        ]
        assert len(cal_calls) >= 1, (
            "Expected calibration command (0xE1 0x08 0x00) during init"
        )

    # ── read() tests ─────────────────────────────────────────────
    def test_read_sends_measurement_trigger(self):
        """read() should send the measurement trigger command: 0xAC 0x33 0x00."""
        self.mock_soft_i2c.readfrom.return_value = bytes([0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])

        sensor = self.DHT20(self.mock_soft_i2c)
        sensor.read()

        trigger_calls = [
            c
            for c in self.mock_soft_i2c.writeto.call_args_list
            if c[0][1] == b"\xac\x33\x00"
        ]
        assert len(trigger_calls) >= 1, (
            "Expected measurement trigger (0xAC 0x33 0x00)"
        )

    def test_read_returns_correct_temperature_and_humidity(self):
        """read() should decode raw bytes into (temp, humidity) floats."""
        # hum_raw = 524288 = 0x80000 → 50.0% humidity
        # temp_raw = 0 → -50.0°C
        # Verified with data sheet formulas
        dummy_data = bytes([0x00, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00])
        self.mock_soft_i2c.readfrom.return_value = dummy_data

        sensor = self.DHT20(self.mock_soft_i2c)
        temp, humidity = sensor.read()

        assert humidity == pytest.approx(50.0, rel=0.01), (
            f"Expected humidity ~50.0%, got {humidity}"
        )
        assert temp == pytest.approx(-50.0, rel=0.01), (
            f"Expected temperature ~-50.0°C, got {temp}"
        )

    def test_read_returns_none_when_sensor_busy(self):
        """read() returns (None, None) when sensor status bit 7 is set."""
        # Status byte with bit 7 set (sensor busy)
        dummy_data = bytes([0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
        self.mock_soft_i2c.readfrom.return_value = dummy_data

        sensor = self.DHT20(self.mock_soft_i2c)
        temp, humidity = sensor.read()

        assert temp is None, f"Expected None temp when busy, got {temp}"
        assert humidity is None, f"Expected None humidity when busy, got {humidity}"

    def test_read_returns_none_when_no_data(self):
        """read() returns (None, None) when I2C returns empty/no data."""
        self.mock_soft_i2c.readfrom.return_value = b""

        sensor = self.DHT20(self.mock_soft_i2c)
        temp, humidity = sensor.read()

        assert temp is None
        assert humidity is None

    def test_read_uses_sensor_address_0x38(self):
        """read() should use the constructor-supplied address (0x38 default)."""
        self.mock_soft_i2c.readfrom.return_value = bytes(7)

        sensor = self.DHT20(self.mock_soft_i2c)
        sensor.read()

        # readfrom should be called with addr=0x38
        read_calls = [
            c for c in self.mock_soft_i2c.readfrom.call_args_list
            if c[0][0] == 0x38
        ]
        assert len(read_calls) >= 1

    # ── Temperature decoding at known values ─────────────────────
    def test_temperature_25c_decoded_correctly(self):
        """At 25°C: temp_raw = (25 + 50) * 1048576 / 200 = 393216."""
        # temp_raw = 393216 = 0x60000
        # Split: data[3] low nibble = 0x06, data[4] = 0x00, data[5] = 0x00
        # hum_raw = 0 (0%): data[1]=0x00, data[2]=0x00, data[3] high nibble=0x00
        # So data[3] = 0x60 (low nibble 0x0? Wait, let me recalculate.)
        #
        # temp_raw = ((data[3] & 0x0F) << 16) | (data[4] << 8) | data[5]
        # For temp_raw = 393216 = 0x60000:
        #   (data[3] & 0x0F) << 16 = 0x60000 → data[3] & 0x0F = 6
        #   data[4] << 8 = 0x0000 → data[4] = 0
        #   data[5] = 0
        #
        # hum_raw = (data[1] << 12) | (data[2] << 4) | (data[3] >> 4)
        # For hum_raw = 0: data[1]=0x00, data[2]=0x00, data[3] >> 4 = 0
        # So data[3] = 0x06 works for both.
        dummy_data = bytes([0x00, 0x00, 0x00, 0x06, 0x00, 0x00, 0x00])
        self.mock_soft_i2c.readfrom.return_value = dummy_data

        sensor = self.DHT20(self.mock_soft_i2c)
        temp, humidity = sensor.read()

        assert temp == pytest.approx(25.0, rel=0.05), (
            f"Expected temperature ~25.0°C, got {temp}"
        )
        assert humidity == pytest.approx(0.0, abs=0.1), (
            f"Expected humidity ~0.0%, got {humidity}"
        )

    def test_humidity_100pct_decoded_correctly(self):
        """At max humidity (99.9999%): hum_raw = 1048575 = 0xFFFFF."""
        # hum_raw = 0xFFFFF: data[1]=0xFF, data[2]=0xFF, data[3]>>4=0xF
        # temp_raw = 0: data[3]&0x0F=0x0, data[4]=0x00, data[5]=0x00
        # So data[3] = 0xF0
        dummy_data = bytes([0x00, 0xFF, 0xFF, 0xF0, 0x00, 0x00, 0x00])
        self.mock_soft_i2c.readfrom.return_value = dummy_data

        sensor = self.DHT20(self.mock_soft_i2c)
        temp, humidity = sensor.read()

        # 1048575/1048576*100 ≈ 99.9999%
        assert humidity == pytest.approx(100.0, rel=0.01), (
            f"Expected humidity ~100.0%, got {humidity}"
        )
        assert temp == pytest.approx(-50.0, rel=0.01), (
            f"Expected temperature ~-50.0°C, got {temp}"
        )


# ── Sensor dispatch tests ───────────────────────────────────────────


class TestSensorDispatch:
    """Unit tests for read_humidity_temperature() dispatch function."""

    @pytest.fixture(autouse=True)
    def setup_mocks(self):
        """Mock machine.SoftI2C and machine.Pin via sys.modules."""
        self.mock_soft_i2c = MagicMock()
        self.mock_soft_i2c.readfrom.return_value = bytes(7)

        sys.modules["machine"] = MagicMock()
        sys.modules["machine"].SoftI2C = MagicMock(return_value=self.mock_soft_i2c)
        sys.modules["machine"].Pin = MagicMock()

        # MicroPython time.sleep_ms doesn't exist on CPython — monkey-patch it
        import time
        if not hasattr(time, "sleep_ms"):
            time.sleep_ms = lambda ms: _real_time.sleep(ms / 1000.0)

        # Pre-import the DHT20 mock module so the dispatch can import it
        mock_dht20 = MagicMock()
        mock_dht20.DHT20 = MagicMock()
        mock_sensor = MagicMock()
        mock_sensor.read.return_value = (25.0, 60.0)
        mock_dht20.DHT20.return_value = mock_sensor
        sys.modules["lib.sensors.dht20"] = mock_dht20

        from cardputer_client.lib.sensors import read_humidity_temperature

        self.read_humidity_temperature = read_humidity_temperature
        yield
        # Cleanup
        for mod in (
            "lib.sensors.dht20",
            "cardputer_client.lib.sensors",
            "cardputer_client.lib.sensors.dht20",
        ):
            if mod in sys.modules:
                del sys.modules[mod]
        if hasattr(time, "sleep_ms"):
            delattr(time, "sleep_ms")

    def test_returns_none_none_when_sensor_type_is_none(self):
        """When sensor_type is None, returns (None, None) without touching I2C."""
        temp, humidity = self.read_humidity_temperature(None)
        assert temp is None
        assert humidity is None

    def test_returns_none_none_for_unknown_sensor_type(self):
        """Unknown sensor types return (None, None) gracefully."""
        temp, humidity = self.read_humidity_temperature("BME280")
        assert temp is None
        assert humidity is None

    def test_dht20_sensor_returns_temperature_and_humidity(self):
        """DHT20 sensor returns (temperature, humidity) from sensor.read()."""
        temp, humidity = self.read_humidity_temperature("DHT20")
        assert temp == 25.0
        assert humidity == 60.0

    def test_dht20_uses_custom_i2c_address(self):
        """Custom I2C address is passed to DHT20 constructor."""
        self.read_humidity_temperature("DHT20", i2c_addr=0x39)
        dht20_class = sys.modules["lib.sensors.dht20"].DHT20
        dht20_class.assert_called_once()
        # Check the I2C address arg
        call_args = dht20_class.call_args
        assert call_args[0][1] == 0x39

    def test_dht20_uses_default_i2c_address_0x38(self):
        """Default I2C address is 0x38."""
        self.read_humidity_temperature("DHT20")
        dht20_class = sys.modules["lib.sensors.dht20"].DHT20
        call_args = dht20_class.call_args
        assert call_args[0][1] == 0x38

    def test_os_error_caught_and_returns_none_none(self):
        """OSError during sensor.read() returns (None, None) gracefully."""
        # Make the mocked DHT20 sensor's read() raise OSError
        mock_dht20 = sys.modules["lib.sensors.dht20"]
        mock_sensor = MagicMock()
        mock_sensor.read.side_effect = OSError("I2C bus error")
        mock_dht20.DHT20.return_value = mock_sensor

        temp, humidity = self.read_humidity_temperature("DHT20")
        assert temp is None
        assert humidity is None

    def test_custom_sda_scl_pins(self):
        """Custom SDA/SCL pins are passed to SoftI2C."""
        self.read_humidity_temperature("DHT20", sda_pin=5, scl_pin=6)
        pin_class = sys.modules["machine"].Pin
        # Should have been called with Pin(5) and Pin(6)
        pin_calls = [c[0][0] for c in pin_class.call_args_list]
        assert 5 in pin_calls, f"Pin(5) not called, got {pin_calls}"
        assert 6 in pin_calls, f"Pin(6) not called, got {pin_calls}"

    def test_import_error_from_dht20_module_propagates(self):
        """ImportError (missing driver module) should propagate, not be silently caught."""
        # Remove the mock dht20 module so import fails
        del sys.modules["lib.sensors.dht20"]

        with pytest.raises(ImportError):
            self.read_humidity_temperature("DHT20")

        # Restore for other tests
        mock_dht20 = MagicMock()
        sys.modules["lib.sensors.dht20"] = mock_dht20
