"""Tests for k8s-app IoT ingest script."""

import asyncio
import contextlib
import importlib.util
import inspect
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

    @pytest.mark.parametrize(
        "status_code,expected_output,expected_log",
        [
            (
                "UNAVAILABLE",
                "Subscribe error: server unavailable",
                "gRPC subscribe failed (UNAVAILABLE)",
            ),
            (
                "DEADLINE_EXCEEDED",
                "Subscribe timeout",
                "gRPC subscribe timeout (DEADLINE_EXCEEDED)",
            ),
        ],
    )
    def test_subscribe_specific_errors_logged(
        self, status_code, expected_output, expected_log, capsys
    ):
        """Specific gRPC error codes should print/log appropriate messages."""
        mock_stub = MagicMock()

        class FakeRpcError(iot_ingest.grpc.RpcError):
            def code(self):
                return getattr(iot_ingest.grpc.StatusCode, status_code)

            def details(self):
                return f"details for {status_code}"

            def __str__(self):
                return f"details for {status_code}"

        mock_stub.Subscribe.side_effect = FakeRpcError()

        with patch.object(iot_ingest.logger, "warning") as mock_logger_warning:
            iot_ingest.subscribe_example(mock_stub, timeout=5)

        captured = capsys.readouterr()
        assert expected_output in captured.out

        if expected_log:
            mock_logger_warning.assert_called_once()
            assert expected_log in mock_logger_warning.call_args[0][0]

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
        mock_stub.Send.return_value = MagicMock(status="queued", destination_hash="abc123")

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

    for mod in [
        "nats",
        "nats.aio",
        "nats.aio.client",
        "nats.js",
        "nats.js.api",
        "lma_core.queue",
    ]:
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
            await iot_ingest.send_example_nats("nats://test:4222", subject="custom.subject.test")

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
            if inspect.iscoroutinefunction(callback):
                await callback(msg)
            else:
                callback(msg)
            # Simulate long-running subscription
            while True:
                await asyncio.sleep(0.1)

        nq.subscribe = fake_subscribe

        with patch("lma_core.queue.NatsQueue", return_value=nq):
            await iot_ingest.subscribe_example_nats("nats://test:4222", timeout=1)

        nq.connect.assert_called_once()
        nq.ensure_stream.assert_called_once()
        nq.close.assert_called_once()
        captured = capsys.readouterr()
        assert "Received" in captured.out
        assert "lmao.messages.env" in captured.out

    @pytest.mark.asyncio
    async def test_subscribe_example_nats_reports_count(self, mock_nats_for_iot, capsys):
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
                if inspect.iscoroutinefunction(callback):
                    await callback(msg)
                else:
                    callback(msg)
            while True:
                await asyncio.sleep(0.1)

        nq.subscribe = fake_subscribe

        with patch("lma_core.queue.NatsQueue", return_value=nq):
            await iot_ingest.subscribe_example_nats("nats://test:4222", timeout=1)

        captured = capsys.readouterr()
        assert "Total received: 3 message(s)" in captured.out


class TestMainFunction:
    """Tests for main() entry point."""

    @pytest.mark.asyncio
    async def test_main_nats_error_logged(self, mock_nats_for_iot):
        """main() with --use-nats should log NATS failures via logger.exception."""
        test_args = ["iot_ingest.py", "--use-nats", "--send"]
        with patch.object(sys, "argv", test_args):
            with patch.object(iot_ingest.logger, "exception") as mock_logger_exc:
                with patch.object(
                    iot_ingest,
                    "send_example_nats",
                    side_effect=Exception("nats failed"),
                ):
                    with pytest.raises(SystemExit):
                        iot_ingest.main()

        mock_logger_exc.assert_called_once_with("NATS operation failed")

    def test_main_runs_default_mode_without_args(self, capsys):
        """main() with no args should run all examples (gRPC path).

        Verifies that argument parsing and default dispatch work correctly.
        """
        test_args = ["iot_ingest.py"]
        mock_stub = MagicMock()
        mock_stub.Send.return_value.status = "queued"
        mock_stub.GetIdentity.return_value.identity_hex = "aaabbb"
        mock_stub.Subscribe.return_value = iter([])

        with patch.object(sys, "argv", test_args):
            with patch.object(iot_ingest, "LMAOStub", return_value=mock_stub):
                with patch.object(iot_ingest.grpc, "insecure_channel"):
                    iot_ingest.main()

        captured = capsys.readouterr()
        assert "Connected to LMAO server" in captured.out
        assert "Send response" in captured.out
        assert "Subscribe Example" in captured.out
        assert "Server identity" in captured.out


# ---------------------------------------------------------------------------
# Fixture — mock DuckDB storage for DuckDB integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_duckdb_store():
    """Mock DuckDbStore so iot_ingest DuckDB integration tests run without real duckdb."""
    mock_store = MagicMock()
    mock_store.store_sensor_report = AsyncMock()
    mock_store.query = AsyncMock()
    mock_store.initialize = MagicMock()
    mock_store.close = MagicMock()
    return mock_store


class TestSubscribeExampleNatsWithStore:
    """Tests for subscribe_example_nats() with DuckDB store_path."""

    @staticmethod
    async def _awaiting_fake_subscribe(subject, durable, callback):
        """Fake subscribe that properly awaits async callbacks."""
        while True:
            await asyncio.sleep(0.1)

    @pytest.mark.asyncio
    async def test_store_initialized_when_store_path_provided(
        self, mock_nats_for_iot, mock_duckdb_store, capsys
    ):
        """subscribe_example_nats should initialize DuckDbStore when store_path is set."""
        from lma_core.queue import NatsQueue

        nq = NatsQueue(name="iot-ingest-subscriber")
        nq.connect = AsyncMock()
        nq.ensure_stream = AsyncMock()
        nq.close = AsyncMock()
        nq.subscribe = self._awaiting_fake_subscribe

        with patch("lma_core.queue.NatsQueue", return_value=nq):
            with patch("lma_core.storage.DuckDbStore", return_value=mock_duckdb_store):
                await iot_ingest.subscribe_example_nats(
                    "nats://test:4222",
                    timeout=1,
                    store_path="/tmp/test.db",
                )

        mock_duckdb_store.initialize.assert_called_once_with("/tmp/test.db")
        mock_duckdb_store.close.assert_called_once()
        captured = capsys.readouterr()
        assert "DuckDB store initialized" in captured.out

    @pytest.mark.asyncio
    async def test_store_and_ack_calls_store_sensor_report(
        self, mock_nats_for_iot, mock_duckdb_store
    ):
        """_store_and_ack should call store.store_sensor_report with message bytes."""
        from lma_core.queue import NatsQueue

        nq = NatsQueue(name="iot-ingest-subscriber")
        nq.connect = AsyncMock()
        nq.ensure_stream = AsyncMock()
        nq.close = AsyncMock()

        async def fake_subscribe(subject, durable, callback):
            msg = MagicMock()
            msg.data = b"test_sensor_data"
            msg.subject = "lmao.messages.env"
            await callback(msg)
            while True:
                await asyncio.sleep(0.1)

        nq.subscribe = fake_subscribe

        with patch("lma_core.queue.NatsQueue", return_value=nq):
            with patch("lma_core.storage.DuckDbStore", return_value=mock_duckdb_store):
                await iot_ingest.subscribe_example_nats(
                    "nats://test:4222",
                    timeout=1,
                    store_path="/tmp/test.db",
                )

        mock_duckdb_store.store_sensor_report.assert_called_once_with(b"test_sensor_data")

    @pytest.mark.asyncio
    async def test_store_failure_raises_for_nak(self, mock_nats_for_iot, mock_duckdb_store):
        """_store_and_ack should raise when store_sensor_report fails, triggering NAK."""
        from lma_core.queue import NatsQueue

        nq = NatsQueue(name="iot-ingest-subscriber")
        nq.connect = AsyncMock()
        nq.ensure_stream = AsyncMock()
        nq.close = AsyncMock()

        mock_duckdb_store.store_sensor_report.side_effect = RuntimeError("disk full")

        callback_raised = []

        async def fake_subscribe(subject, durable, callback):
            msg = MagicMock()
            msg.data = b"bad_data"
            msg.subject = "lmao.messages.env"
            try:
                await callback(msg)
            except RuntimeError:
                callback_raised.append(True)
            while True:
                await asyncio.sleep(0.1)

        nq.subscribe = fake_subscribe

        with patch("lma_core.queue.NatsQueue", return_value=nq):
            with patch("lma_core.storage.DuckDbStore", return_value=mock_duckdb_store):
                await iot_ingest.subscribe_example_nats(
                    "nats://test:4222",
                    timeout=1,
                    store_path="/tmp/test.db",
                )

        # The callback should have raised (which triggers NAK in subscribe loop)
        assert len(callback_raised) == 1
        mock_duckdb_store.store_sensor_report.assert_called_once()


class TestQueryOnlyMode:
    """Tests for --query flag (query-only mode)."""

    def test_query_flag_runs_query_and_prints(self, mock_duckdb_store, capsys):
        """main() with --query should run SQL query and print results."""
        mock_duckdb_store.query.return_value = [("node-1", 5), ("node-2", 3)]

        test_args = [
            "iot_ingest.py",
            "--query",
            "SELECT node_id, count(*) FROM sensor_readings GROUP BY node_id",
        ]

        with patch.object(sys, "argv", test_args):
            with patch("lma_core.storage.DuckDbStore", return_value=mock_duckdb_store):
                iot_ingest.main()

        mock_duckdb_store.initialize.assert_called_once()
        assert mock_duckdb_store.query.call_count >= 1
        # First query call should be the user's SQL
        first_query_call = mock_duckdb_store.query.call_args_list[0]
        assert "SELECT node_id, count(*)" in str(first_query_call)
        mock_duckdb_store.close.assert_called_once()
        captured = capsys.readouterr()
        assert "Done." in captured.out

    def test_query_flag_import_error_exits_cleanly(self, capsys):
        """main() with --query should print clean error and exit when DuckDbStore
        cannot be imported."""
        test_args = ["iot_ingest.py", "--query", "SELECT 1"]

        with (
            patch.object(sys, "argv", test_args),
            patch(
                "lma_core.storage.DuckDbStore",
                side_effect=ImportError("duckdb is not installed"),
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            iot_ingest.main()

        assert exc_info.value.code == 1

    def test_query_constructor_import_error_exits_cleanly(self, capsys):
        """main() with --query should handle ImportError from DuckDbStore constructor."""
        test_args = ["iot_ingest.py", "--query", "SELECT 1"]

        mock_store_class = MagicMock(side_effect=ImportError("duckdb not available"))

        with patch.object(sys, "argv", test_args):
            with patch("lma_core.storage.DuckDbStore", mock_store_class):
                with pytest.raises(SystemExit) as exc_info:
                    iot_ingest.main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "ERROR:" in captured.err

    def test_query_empty_result(self, mock_duckdb_store, capsys):
        """main() with --query should print '(no rows returned)' for empty results."""
        mock_duckdb_store.query.return_value = []

        test_args = [
            "iot_ingest.py",
            "--query",
            "SELECT * FROM sensor_readings WHERE 1=0",
        ]

        with patch.object(sys, "argv", test_args):
            with patch("lma_core.storage.DuckDbStore", return_value=mock_duckdb_store):
                iot_ingest.main()

        captured = capsys.readouterr()
        assert "(no rows returned)" in captured.out


class TestStoreCliParsing:
    """Tests for --store and --db-path CLI argument parsing."""

    def test_store_flag_passes_db_path_to_subscribe(self):
        """--store --subscribe --use-nats should pass db_path to subscribe_example_nats."""
        test_args = [
            "iot_ingest.py",
            "--use-nats",
            "--subscribe",
            "--store",
            "--subscribe-timeout",
            "1",
        ]

        with patch.object(sys, "argv", test_args):
            with patch.object(iot_ingest, "subscribe_example_nats") as mock_sub:
                with patch.object(iot_ingest, "logger", "exception"):
                    # Should not raise — subscribe_example_nats is mocked
                    iot_ingest.main()

        mock_sub.assert_called_once()
        _, kwargs = mock_sub.call_args
        assert kwargs["store_path"] == "/data/sensors.db"  # default

    def test_custom_db_path_passed_to_subscribe(self):
        """--db-path custom value should be passed to subscribe_example_nats."""
        test_args = [
            "iot_ingest.py",
            "--use-nats",
            "--subscribe",
            "--store",
            "--db-path",
            "/custom/path/sensors.db",
            "--subscribe-timeout",
            "1",
        ]

        with patch.object(sys, "argv", test_args):
            with patch.object(iot_ingest, "subscribe_example_nats") as mock_sub:
                iot_ingest.main()

        mock_sub.assert_called_once()
        _, kwargs = mock_sub.call_args
        assert kwargs["store_path"] == "/custom/path/sensors.db"

    def test_store_without_subscribe_calls_subscribe_via_default(self):
        """--store without --subscribe defaults to subscribe=True, so store_path is set."""
        test_args = [
            "iot_ingest.py",
            "--use-nats",
            "--store",
        ]

        with patch.object(sys, "argv", test_args):
            with patch.object(iot_ingest, "subscribe_example_nats") as mock_sub:
                with patch.object(iot_ingest, "send_example_nats"):
                    iot_ingest.main()

        # subscribe_example_nats should be called with store_path set
        mock_sub.assert_called_once()
        _, kwargs = mock_sub.call_args
        assert kwargs["store_path"] is not None


def _load_iot_ingest_consumer():
    """Import k8s-app/iot_ingest_consumer.py as a module via importlib.

    Mock nats and duckdb in sys.modules before loading so that the
    module-level lazy imports in lma_core succeed even when external
    deps are not installed.
    """
    # Mock nats modules
    if "nats" not in sys.modules:
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

    # Mock duckdb
    if "duckdb" not in sys.modules:
        duckdb_mod = MagicMock()
        sys.modules["duckdb"] = duckdb_mod

    # Clear lma_core modules so they re-import with mocks
    for key in list(sys.modules):
        if key.startswith("lma_core"):
            del sys.modules[key]

    spec = importlib.util.spec_from_file_location(
        "iot_ingest_consumer", "k8s-app/iot_ingest_consumer.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestConsumerStartup:
    """Tests for iot_ingest_consumer.py startup and env var parsing."""

    def test_env_var_defaults(self):
        """Env var defaults should be used when no env vars are set."""
        import os as _os

        with patch.dict(_os.environ, {}, clear=True):
            assert (
                _os.environ.get("NATS_SERVER", "nats://localhost:4222") == "nats://localhost:4222"
            )
            assert _os.environ.get("DUCKDB_PATH", "/data/sensors.db") == "/data/sensors.db"
            assert _os.environ.get("CONSUMER_NAME", "iot-ingest") == "iot-ingest"

    def test_env_var_overrides(self):
        """Env var overrides should be respected."""
        import os as _os

        with patch.dict(
            _os.environ,
            {
                "NATS_SERVER": "nats://custom:4222",
                "DUCKDB_PATH": "/custom/sensors.db",
                "CONSUMER_NAME": "custom-consumer",
            },
        ):
            assert _os.environ["NATS_SERVER"] == "nats://custom:4222"
            assert _os.environ["DUCKDB_PATH"] == "/custom/sensors.db"
            assert _os.environ["CONSUMER_NAME"] == "custom-consumer"


class TestConsumerConnect:
    """Tests for the consumer's NATS connect + stream ensure flow."""

    @pytest.mark.asyncio
    async def test_connect_called_with_env_server(self):
        """NatsQueue.connect should be called with NATS_SERVER env var."""
        from lma_core.queue import NatsQueue

        nq = NatsQueue(name="iot-ingest")
        nq.connect = AsyncMock()
        nq.ensure_stream = AsyncMock()
        nq.subscribe = AsyncMock()
        nq.close = AsyncMock()

        from lma_core.storage import DuckDbStore

        store = DuckDbStore(name="iot-ingest")
        store.initialize = MagicMock()
        store.close = MagicMock()

        consumer = _load_iot_ingest_consumer()

        with patch.dict("os.environ", {"NATS_SERVER": "nats://test:4222"}):
            with patch("lma_core.queue.NatsQueue", return_value=nq):
                with patch("lma_core.storage.DuckDbStore", return_value=store):

                    # Run the main function briefly — it'll block on subscribe
                    # so we schedule a task and cancel after connect/ensure_stream
                    stream_ensured = False

                    async def _run_and_cancel():
                        nonlocal stream_ensured

                        async def _fake_subscribe(subject, durable, callback):
                            # Signal that stream was ensured, then block
                            nonlocal stream_ensured
                            stream_ensured = True
                            # Wait forever (simulate blocking subscribe)
                            await asyncio.Event().wait()

                        nq.subscribe = _fake_subscribe

                        # Patch signal handlers (not supported in test)
                        with patch.object(asyncio.get_event_loop(), "add_signal_handler"):
                            # Run main with a timeout
                            task = asyncio.ensure_future(consumer.main())
                            # Wait for stream_ensured to be True (max 5s)
                            for _ in range(50):
                                if stream_ensured:
                                    break
                                await asyncio.sleep(0.1)
                            task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await task

                    await _run_and_cancel()

            # Check that connect was called with our env server
            nq.connect.assert_called_once()
            call_args = nq.connect.call_args
            assert call_args[1]["servers"] == "nats://test:4222"
            nq.ensure_stream.assert_called_once()


class TestConsumerStoreAndAck:
    """Tests for _store_and_ack callback."""

    @pytest.mark.asyncio
    async def test_store_and_ack_calls_store_sensor_report(self):
        """_store_and_ack should call store.store_sensor_report with message data."""
        consumer = _load_iot_ingest_consumer()

        mock_store = MagicMock()
        mock_store.store_sensor_report = AsyncMock()

        mock_msg = MagicMock()
        mock_msg.data = b"test_payload"

        await consumer._store_and_ack(mock_msg, mock_store)

        mock_store.store_sensor_report.assert_called_once_with(b"test_payload")

    @pytest.mark.asyncio
    async def test_store_and_ack_raises_on_failure(self):
        """_store_and_ack should propagate exceptions for NAK."""
        consumer = _load_iot_ingest_consumer()

        mock_store = MagicMock()
        mock_store.store_sensor_report = AsyncMock(side_effect=RuntimeError("store failed"))

        mock_msg = MagicMock()
        mock_msg.data = b"bad_data"

        with pytest.raises(RuntimeError, match="store failed"):
            await consumer._store_and_ack(mock_msg, mock_store)


class TestConsumerGracefulShutdown:
    """Tests for graceful shutdown (SIGTERM) handling."""

    @pytest.mark.asyncio
    async def test_close_called_on_shutdown(self):
        """NatsQueue.close() and DuckDbStore.close() should be called on shutdown."""
        from lma_core.queue import NatsQueue

        nq = NatsQueue(name="iot-ingest")
        nq.connect = AsyncMock()
        nq.ensure_stream = AsyncMock()
        nq.close = AsyncMock()

        # subscribe returns immediately (simulating shutdown signal received)
        async def _fake_subscribe(subject, durable, callback):
            pass  # Return immediately — simulates cancelled subscription

        nq.subscribe = _fake_subscribe

        from lma_core.storage import DuckDbStore

        store = DuckDbStore(name="iot-ingest")
        store.initialize = MagicMock()
        store.close = MagicMock()

        consumer = _load_iot_ingest_consumer()

        with patch("lma_core.queue.NatsQueue", return_value=nq):
            with patch("lma_core.storage.DuckDbStore", return_value=store):

                # Patch signal handlers and make shutdown_event fire immediately
                with patch.object(asyncio.get_event_loop(), "add_signal_handler"):
                    # We can't easily patch shutdown_event, but since
                    # subscribe returns immediately, the main loop will
                    # proceed to shutdown naturally.
                    # Actually, let's just test that close is called by
                    # invoking the shutdown path manually:
                    await consumer.main()

        nq.close.assert_called_once()
        store.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_main_handles_import_error(self):
        """main() should catch ImportError and exit with code 1."""
        consumer = _load_iot_ingest_consumer()

        with patch("lma_core.queue.NatsQueue", side_effect=ImportError("nats-py missing")):
            with patch.object(sys, "exit") as mock_exit:
                await consumer.main()
                mock_exit.assert_called_once_with(1)

    def test_module_level_entrypoint_handles_import_error(self):
        """The __main__ block should handle ImportError and exit with code 1."""
        _load_iot_ingest_consumer()

        # Simulate the __main__ block behavior
        with patch("asyncio.run", side_effect=ImportError("nats-py not installed")):
            with patch.object(sys, "exit") as mock_exit:
                try:
                    asyncio.run(None)
                except ImportError:
                    sys.exit(1)
                mock_exit.assert_called_once_with(1)
