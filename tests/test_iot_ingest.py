"""Tests for k8s-app IoT ingest script."""

import asyncio
import importlib.util
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# Load k8s-app/iot_ingest.py via importlib (directory name has a hyphen,
# so it's not a valid Python package name and can't be imported with
# a regular 'from k8s_app import iot_ingest' statement).
def _load_iot_ingest():
    """Import k8s-app/iot_ingest.py as a module via importlib.

    Mock grpc and proto stubs in sys.modules before loading so that the
    module-level imports in iot_ingest.py succeed even when grpcio is not
    installed (the actual gRPC calls are mocked in individual tests).
    """
    if "grpc" not in sys.modules:
        _grpc_mock = MagicMock()
        # RpcError must be a real class so tests can subclass it.
        _grpc_mock.RpcError = type("RpcError", (Exception,), {})
        _grpc_mock.StatusCode = MagicMock()
        _grpc_mock.StatusCode.CANCELLED = "CANCELLED"
        _grpc_mock.StatusCode.UNAVAILABLE = "UNAVAILABLE"
        sys.modules["grpc"] = _grpc_mock

    # proto.lma_pb2_grpc is a checked-in generated file not covered by
    # Bazel's py_proto_library rule. Mock it so that lma_core imports
    # LMAOStub/LMAOServicer successfully.
    if "proto.lma_pb2_grpc" not in sys.modules:
        sys.modules["proto.lma_pb2_grpc"] = MagicMock()

    spec = importlib.util.spec_from_file_location("iot_ingest", "k8s-app/iot_ingest.py")
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


class TestSubscribeExample:
    """Tests for subscribe_example() timeout and CANCELLED wiring."""

    def test_subscribe_passes_timeout_to_stub(self, capsys):
        """subscribe_example should pass timeout parameter to stub.Subscribe."""
        mock_stub = MagicMock()
        # Make Subscribe return an empty iterator (no messages)
        mock_stub.Subscribe.return_value = iter([])

        iot_ingest.subscribe_example(mock_stub, timeout=10)

        # Verify Subscribe was called with timeout=10
        mock_stub.Subscribe.assert_called_once()
        _, kwargs = mock_stub.Subscribe.call_args
        assert kwargs.get("timeout") == 10

    def test_subscribe_cancelled_logged(self, capsys):
        """CANCELLED grpc.RpcError should print CANCELLED message."""
        mock_stub = MagicMock()

        # Create a mock RpcError with CANCELLED code (must inherit from grpc.RpcError
        # to be caught by `except grpc.RpcError` in subscribe_example)
        class FakeRpcError(iot_ingest.grpc.RpcError):
            def code(self):
                return iot_ingest.grpc.StatusCode.CANCELLED

            def details(self):
                return ""

        mock_stub.Subscribe.side_effect = FakeRpcError()

        iot_ingest.subscribe_example(mock_stub, timeout=5)

        captured = capsys.readouterr()
        assert "CANCELLED" in captured.out

    def test_subscribe_other_error_logged(self, capsys):
        """Non-CANCELLED grpc.RpcError should print the error message."""
        mock_stub = MagicMock()

        class FakeRpcError(iot_ingest.grpc.RpcError):
            def code(self):
                return iot_ingest.grpc.StatusCode.UNAVAILABLE

            def details(self):
                return "Service unavailable"

            def __str__(self):
                return "Service unavailable"

        mock_stub.Subscribe.side_effect = FakeRpcError()

        iot_ingest.subscribe_example(mock_stub, timeout=5)

        captured = capsys.readouterr()
        assert "Subscribe error" in captured.out

    def test_subscribe_receives_message(self, capsys):
        """subscribe_example should print received message bytes."""
        mock_stub = MagicMock()

        # Create a mock message
        mock_msg = MagicMock()
        mock_msg.source_hash = "abcdef1234"
        mock_msg.envelope = b"test"

        # Return one message then stop
        mock_stub.Subscribe.return_value = iter([mock_msg])

        iot_ingest.subscribe_example(mock_stub, timeout=5)

        captured = capsys.readouterr()
        assert "Received" in captured.out
        assert "abcdef1234" in captured.out


class TestSendExample:
    """Tests for send_example()."""

    def test_send_example_calls_stub(self, capsys):
        """send_example should call stub.Send with a valid request."""
        mock_stub = MagicMock()
        mock_stub.Send.return_value = MagicMock(
            status="queued", destination_hash="abc123"
        )

        iot_ingest.send_example(mock_stub)

        mock_stub.Send.assert_called_once()
        captured = capsys.readouterr()
        assert "queued" in captured.out


class TestGetIdentityExample:
    """Tests for get_identity_example()."""

    def test_get_identity_calls_stub(self, capsys):
        """get_identity_example should call stub.GetIdentity."""
        mock_stub = MagicMock()
        mock_stub.GetIdentity.return_value = MagicMock(
            identity_hex="aaabbbccc", node_name="lmao-server"
        )

        iot_ingest.get_identity_example(mock_stub)

        mock_stub.GetIdentity.assert_called_once()
        captured = capsys.readouterr()
        assert "aaabbbccc" in captured.out
        assert "lmao-server" in captured.out


# ---------------------------------------------------------------------------
# Fixture — mock nats modules for NATS path tests
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_nats_for_iot():
    """Populate sys.modules with mocks for nats-py so iot_ingest NATS
    functions can import lma_core.queue.NatsQueue successfully."""
    nats_mod = types.ModuleType("nats")
    nats_aio_mod = types.ModuleType("nats.aio")
    nats_aio_client_mod = types.ModuleType("nats.aio.client")
    nats_js_mod = types.ModuleType("nats.js")
    nats_js_api_mod = types.ModuleType("nats.js.api")

    nats_mod.connect = AsyncMock()
    nats_aio_client_mod.Client = MagicMock()
    nats_js_mod.JetStreamContext = MagicMock()
    nats_js_mod.api = nats_js_api_mod

    sys.modules["nats"] = nats_mod
    sys.modules["nats.aio"] = nats_aio_mod
    sys.modules["nats.aio.client"] = nats_aio_client_mod
    sys.modules["nats.js"] = nats_js_mod
    sys.modules["nats.js.api"] = nats_js_api_mod

    # Clear lma_core.queue so it re-imports with mocked nats
    for key in list(sys.modules):
        if key.startswith("lma_core.queue"):
            del sys.modules[key]

    yield

    for mod in ["nats", "nats.aio", "nats.aio.client", "nats.js", "nats.js.api", "lma_core.queue"]:
        if mod in sys.modules:
            del sys.modules[mod]


class TestSendExampleNats:
    """Tests for send_example_nats()."""

    @pytest.mark.asyncio
    async def test_send_example_nats_calls_publish(self, mock_nats_for_iot, capsys):
        """send_example_nats should connect, ensure stream, publish, and close."""
        from lma_core.queue import NatsQueue

        nq = NatsQueue(name="iot-ingest-sender")
        nq.connect = AsyncMock()
        nq.ensure_stream = AsyncMock()
        nq.publish = AsyncMock(return_value=MagicMock(seq=42))
        nq.close = AsyncMock()

        with patch("lma_core.queue.NatsQueue", return_value=nq):
            await iot_ingest.send_example_nats("nats://test:4222")

        nq.connect.assert_called_once_with(servers="nats://test:4222")
        nq.ensure_stream.assert_called_once_with("LMAO_MESSAGES", ["lmao.messages.>"])
        nq.publish.assert_called_once()
        nq.close.assert_called_once()
        captured = capsys.readouterr()
        assert "seq=42" in captured.out

    @pytest.mark.asyncio
    async def test_send_example_nats_uses_custom_subject(self, mock_nats_for_iot, capsys):
        """send_example_nats should publish to the custom subject provided."""
        from lma_core.queue import NatsQueue

        nq = NatsQueue(name="iot-ingest-sender")
        nq.connect = AsyncMock()
        nq.ensure_stream = AsyncMock()
        nq.publish = AsyncMock(return_value=MagicMock(seq=99))
        nq.close = AsyncMock()

        with patch("lma_core.queue.NatsQueue", return_value=nq):
            await iot_ingest.send_example_nats(
                "nats://test:4222", subject="custom.subject.test"
            )

        # Publish should use the custom subject, but stream filter stays fixed
        nq.publish.assert_called_once()
        call_args = nq.publish.call_args
        assert call_args[0][0] == "custom.subject.test"
        nq.ensure_stream.assert_called_once_with("LMAO_MESSAGES", ["lmao.messages.>"])


class TestSubscribeExampleNats:
    """Tests for subscribe_example_nats()."""

    @pytest.mark.asyncio
    async def test_subscribe_example_nats_creates_subscription_and_cancels(
        self, mock_nats_for_iot, capsys
    ):
        """subscribe_example_nats should subscribe, receive a message, and cancel."""
        from lma_core.queue import NatsQueue

        nq = NatsQueue(name="iot-ingest-subscriber")
        nq.connect = AsyncMock()
        nq.ensure_stream = AsyncMock()
        nq.close = AsyncMock()

        async def fake_subscribe(subject, durable, callback):
            msg = MagicMock()
            msg.data = b"hello"
            msg.subject = "lmao.messages.env"
            callback(msg)
            # Simulate long-running subscription
            while True:
                await asyncio.sleep(0.1)

        nq.subscribe = fake_subscribe

        with patch("lma_core.queue.NatsQueue", return_value=nq):
            await iot_ingest.subscribe_example_nats(
                "nats://test:4222", timeout=1
            )

        nq.connect.assert_called_once()
        nq.ensure_stream.assert_called_once()
        nq.close.assert_called_once()
        captured = capsys.readouterr()
        assert "Received" in captured.out
        assert "lmao.messages.env" in captured.out

    @pytest.mark.asyncio
    async def test_subscribe_example_nats_reports_count(
        self, mock_nats_for_iot, capsys
    ):
        """subscribe_example_nats should report total received message count."""
        from lma_core.queue import NatsQueue

        nq = NatsQueue(name="iot-ingest-subscriber")
        nq.connect = AsyncMock()
        nq.ensure_stream = AsyncMock()
        nq.close = AsyncMock()

        async def fake_subscribe(subject, durable, callback):
            for i in range(3):
                msg = MagicMock()
                msg.data = f"msg{i}".encode()
                msg.subject = f"lmao.messages.{i}"
                callback(msg)
            while True:
                await asyncio.sleep(0.1)

        nq.subscribe = fake_subscribe

        with patch("lma_core.queue.NatsQueue", return_value=nq):
            await iot_ingest.subscribe_example_nats(
                "nats://test:4222", timeout=1
            )

        captured = capsys.readouterr()
        assert "Total received: 3 message(s)" in captured.out
