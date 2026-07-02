"""Tests for k8s-app IoT ingest script."""
import importlib.util
import pytest


# Load k8s-app/iot_ingest.py via importlib (directory name has a hyphen,
# so it's not a valid Python package name and can't be imported with
# a regular 'from k8s_app import iot_ingest' statement).
def _load_iot_ingest():
    """Import k8s-app/iot_ingest.py as a module via importlib."""
    spec = importlib.util.spec_from_file_location(
        "iot_ingest", "k8s-app/iot_ingest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Module-level import (cached after first load)
iot_ingest = _load_iot_ingest()


class TestBuildSensorEnvelope:
    """Unit tests for build_sensor_envelope()."""

    def test_returns_bytes(self):
        """build_sensor_envelope should return serialized bytes."""
        result = iot_ingest.build_sensor_envelope("test-node", 25.0, 60.0)
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_envelope_roundtrip(self):
        """Round-trip: build → parse should recover sensor data."""
        node_id = "roundtrip-node-01"
        temp = 23.5
        humidity = 55.0

        payload = iot_ingest.build_sensor_envelope(node_id, temp, humidity)

        # Parse back
        from proto import lma_pb2
        envelope = lma_pb2.LMAOEnvelope()
        envelope.ParseFromString(payload)

        assert envelope.sensor.node_id == node_id
        assert envelope.sensor.battery == pytest.approx(3.7)
        assert len(envelope.sensor.readings) == 2

    def test_sensor_readings_structure(self):
        """Verify sensor readings have correct fields and types."""
        payload = iot_ingest.build_sensor_envelope("struct-node", 30.0, 80.0)

        from proto import lma_pb2
        envelope = lma_pb2.LMAOEnvelope()
        envelope.ParseFromString(payload)

        readings = envelope.sensor.readings
        assert len(readings) == 2

        # First reading: temperature
        temp_reading = readings[0]
        assert temp_reading.sensor_id == 1
        assert temp_reading.unit == "C"
        assert temp_reading.value == pytest.approx(30.0)

        # Second reading: humidity
        hum_reading = readings[1]
        assert hum_reading.sensor_id == 2
        assert hum_reading.unit == "%"
        assert hum_reading.value == pytest.approx(80.0)

    def test_default_battery_value(self):
        """Battery should default to 3.7V."""
        payload = iot_ingest.build_sensor_envelope("batt-test", 20.0, 50.0)

        from proto import lma_pb2
        envelope = lma_pb2.LMAOEnvelope()
        envelope.ParseFromString(payload)

        assert envelope.sensor.battery == pytest.approx(3.7)

    def test_negative_temperature(self):
        """Should handle sub-zero temperatures."""
        payload = iot_ingest.build_sensor_envelope("freezer", -15.0, 40.0)

        from proto import lma_pb2
        envelope = lma_pb2.LMAOEnvelope()
        envelope.ParseFromString(payload)

        assert envelope.sensor.readings[0].value == pytest.approx(-15.0)

    def test_zero_humidity(self):
        """Should handle zero humidity."""
        payload = iot_ingest.build_sensor_envelope("dry", 30.0, 0.0)

        from proto import lma_pb2
        envelope = lma_pb2.LMAOEnvelope()
        envelope.ParseFromString(payload)

        assert envelope.sensor.readings[1].value == pytest.approx(0.0)
