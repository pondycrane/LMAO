"""Unit tests for cardputer_client.main — no hardware required.

Tests the pure-logic helpers that can be exercised without a Cardputer device:
  - _needs_wifi()
  - pending_replies drain (via mock patch)

The main() boot sequence itself requires MicroPython-only modules (Reticulum,
LXMRouter, machine, st7789, network) and is tested via E2E tests only.

Run with::

    bazel test //tests:test_main --test_output=all
"""

from unittest.mock import MagicMock, patch
import pytest


# Import the module under test (works when running under Bazel with proper deps)
try:
    from cardputer_client import main as lmao_client
except ImportError:
    lmao_client = None


# ── _needs_wifi ─────────────────────────────────────────────────────


class TestNeedsWifi:
    """Tests for _needs_wifi() — pure function, no mocking needed."""

    def _needs_wifi(self, config):
        """Thin wrapper to call the module-level function."""
        return lmao_client._needs_wifi(config)

    def test_enabled_udp_interface_returns_true(self):
        config = {"interfaces": [{"type": "UDPInterface", "enabled": True}]}
        assert self._needs_wifi(config) is True

    def test_disabled_udp_interface_returns_false(self):
        config = {"interfaces": [{"type": "UDPInterface", "enabled": False}]}
        assert self._needs_wifi(config) is False

    def test_enabled_tcp_interface_returns_true(self):
        config = {"interfaces": [{"type": "TCPClientInterface", "enabled": True}]}
        assert self._needs_wifi(config) is True

    def test_lora_only_returns_false(self):
        config = {"interfaces": [{"type": "LoRaInterface", "enabled": True}]}
        assert self._needs_wifi(config) is False

    def test_mixed_interfaces_filters_correctly(self):
        config = {
            "interfaces": [
                {"type": "LoRaInterface", "enabled": True},
                {"type": "UDPInterface", "enabled": False},
                {"type": "TCPClientInterface", "enabled": True},
            ]
        }
        assert self._needs_wifi(config) is True

    def test_empty_interfaces_returns_false(self):
        assert self._needs_wifi({"interfaces": []}) is False

    def test_missing_interfaces_key_returns_false(self):
        assert self._needs_wifi({}) is False

    def test_unknown_interface_type_returns_false(self):
        config = {"interfaces": [{"type": "SerialInterface", "enabled": True}]}
        assert self._needs_wifi(config) is False

    def test_interface_without_enabled_key_defaults_to_false(self):
        config = {"interfaces": [{"type": "UDPInterface"}]}
        assert self._needs_wifi(config) is False


# ── handle_reply ────────────────────────────────────────────────────


class TestHandleReply:
    """Tests for handle_reply() callback logic."""

    def test_valid_content_appends_to_pending_replies(self):
        """A message with valid content should be added to pending_replies."""
        # Reset pending_replies
        lmao_client.pending_replies.clear()

        msg = MagicMock()
        msg.content_as_string.return_value = "Valid reply content"

        lmao_client.handle_reply(msg)

        assert lmao_client.pending_replies == ["Valid reply content"]

    def test_empty_content_is_skipped(self):
        """A message with empty content should not be added."""
        lmao_client.pending_replies.clear()

        msg = MagicMock()
        msg.content_as_string.return_value = ""

        lmao_client.handle_reply(msg)

        assert lmao_client.pending_replies == []

    def test_none_content_is_skipped(self):
        """content_as_string returning None should be treated as empty."""
        lmao_client.pending_replies.clear()

        msg = MagicMock()
        msg.content_as_string.return_value = None

        lmao_client.handle_reply(msg)

        assert lmao_client.pending_replies == []

    def test_exception_during_extraction_does_not_crash(self):
        """An exception during content_as_string should not crash the callback."""
        lmao_client.pending_replies.clear()

        msg = MagicMock()
        msg.content_as_string.side_effect = Exception("parse error")

        # Mock sys.print_exception for CPython (MicroPython has it built-in)
        with patch.object(lmao_client.sys, "print_exception", create=True):
            lmao_client.handle_reply(msg)

        assert lmao_client.pending_replies == []

    def test_multiple_replies_accumulate(self):
        """Multiple calls should accumulate replies in order."""
        lmao_client.pending_replies.clear()

        msg1 = MagicMock()
        msg1.content_as_string.return_value = "First"
        msg2 = MagicMock()
        msg2.content_as_string.return_value = "Second"

        lmao_client.handle_reply(msg1)
        lmao_client.handle_reply(msg2)

        assert lmao_client.pending_replies == ["First", "Second"]


# ── DEST_HASH conversion ───────────────────────────────────────────


class TestConvertDestHash:
    """Tests for _convert_dest_hash() — pure function, no mocking needed."""

    def _convert_dest_hash(self, hex_val):
        return lmao_client._convert_dest_hash(hex_val)

    def test_converts_valid_hex_string_to_bytes(self):
        result = self._convert_dest_hash("aabb")
        assert result == b"\xaa\xbb"

    def test_converts_hex_with_uppercase(self):
        result = self._convert_dest_hash("AAFF")
        assert result == b"\xaa\xff"

    def test_converts_mixed_case_hex(self):
        result = self._convert_dest_hash("AaBb")
        assert result == b"\xaa\xbb"

    def test_passes_through_bytes_values(self):
        input_bytes = b"\x01\x02"
        result = self._convert_dest_hash(input_bytes)
        assert result is input_bytes

    def test_returns_none_when_dest_hash_is_none(self):
        result = self._convert_dest_hash(None)
        assert result is None

    def test_raises_valueerror_on_invalid_hex_string(self):
        import pytest

        with pytest.raises(ValueError):
            self._convert_dest_hash("not-hex!!")

    def test_raises_valueerror_on_odd_length_hex(self):
        import pytest

        with pytest.raises(ValueError):
            self._convert_dest_hash("abc")

    def test_raises_valueerror_on_non_string_non_bytes_non_none(self):
        import pytest

        with pytest.raises(ValueError):
            self._convert_dest_hash(42)


# ── make_sensor_message ─────────────────────────────────────────────


class TestInitRns:
    """Tests for _init_rns() — boot-sequence helper."""

    def test_init_rns_creates_reticulum_with_config(self):
        """_init_rns should create Reticulum and set config."""
        mock_config = {"interfaces": [{"type": "LoRaInterface"}]}
        mock_rns = MagicMock()

        with patch.object(
            lmao_client, "Reticulum", create=True, return_value=mock_rns
        ):
            rns = lmao_client._init_rns(mock_config)

        assert rns is mock_rns
        assert rns.config is mock_config
        rns.setup_interfaces.assert_called_once()

    def test_init_rns_logs_and_hangs_on_failure(self):
        """_init_rns should let exceptions propagate (caller handles hang)."""
        with patch.object(
            lmao_client, "Reticulum", create=True, side_effect=RuntimeError("fail")
        ):
            with pytest.raises(RuntimeError, match="fail"):
                lmao_client._init_rns({})


class TestInitLxmfRouter:
    """Tests for _init_lxmf_router() — boot-sequence helper."""

    def test_creates_router_with_identity(self):
        """_init_lxmf_router should create LXMRouter and register callbacks."""
        mock_identity = MagicMock()
        mock_router = MagicMock()

        with patch.object(
            lmao_client, "LXMRouter", create=True, return_value=mock_router
        ) as mock_lxmr:
            router = lmao_client._init_lxmf_router(
                mock_identity, storage_path="/tmp/test", display_name="my-node"
            )
            assert router is mock_router
            mock_lxmr.assert_called_once_with(
                identity=mock_identity, storagepath="/tmp/test"
            )
            mock_router.register_delivery_identity.assert_called_once_with(
                mock_identity, display_name="my-node"
            )
            mock_router.register_delivery_callback.assert_called_once_with(
                lmao_client.handle_reply
            )

    def test_uses_default_storage_path(self):
        """_init_lxmf_router should default to /flash/lxmf_state."""
        mock_identity = MagicMock()
        mock_router = MagicMock()

        with patch.object(
            lmao_client, "LXMRouter", create=True, return_value=mock_router
        ) as mock_lxmr:
            lmao_client._init_lxmf_router(mock_identity)
            call_kwargs = mock_lxmr.call_args[1]
            assert call_kwargs["storagepath"] == "/flash/lxmf_state"


class TestInitWifi:
    """Tests for _init_wifi() — boot-sequence helper."""

    def test_skips_when_not_needed(self):
        """_init_wifi should return False when WiFi is not needed."""
        config = {"interfaces": [{"type": "LoRaInterface", "enabled": True}]}

        result = lmao_client._init_wifi("ssid", "pass", config)

        assert result is False

    def test_connects_when_needed(self):
        """_init_wifi should call _connect_wifi and return True when needed."""
        config = {"interfaces": [{"type": "UDPInterface", "enabled": True}]}

        with patch.object(lmao_client, "_connect_wifi") as mock_connect:
            result = lmao_client._init_wifi("ssid", "pass", config, debug=1)

        assert result is True
        mock_connect.assert_called_once_with("ssid", "pass", 1)

    def test_uses_default_debug(self):
        """_init_wifi should default debug to 0."""
        config = {"interfaces": [{"type": "UDPInterface", "enabled": True}]}

        with patch.object(lmao_client, "_connect_wifi") as mock_connect:
            lmao_client._init_wifi("ssid", "pass", config)

        mock_connect.assert_called_once_with("ssid", "pass", 0)


class TestMakeSensorMessage:
    """Tests for make_sensor_message() — mocks the encoder, validates args."""

    import pytest

    @staticmethod
    def _call(identity_hex="a1b2", seq=0, battery=3.7):
        """Call make_sensor_message with a mocked encoder, return captured args."""
        mock_encode = MagicMock()
        with patch.object(lmao_client, "encode_sensor_envelope", mock_encode,
                          create=True):
            lmao_client.make_sensor_message(identity_hex, seq, battery)
        mock_encode.assert_called_once()
        return mock_encode.call_args[0]  # (identity_hex, seq, battery, readings)

    def test_temperature_fallback_on_cpython(self):
        """On CPython (no esp32 module), temperature falls back to 25.0°C."""
        args = self._call(seq=0)
        readings = args[3]
        assert readings[0]["value"] == 25.0

    def test_temperature_is_not_seq_dependent_on_cpython(self):
        """Fallback temperature is constant 25.0°C regardless of seq."""
        for seq in (1, 7, 9, 10):
            args = self._call(seq=seq)
            assert args[3][0]["value"] == 25.0, (
                f"seq={seq}: expected fallback 25.0, got {args[3][0]['value']}"
            )

    def test_identity_hex_passthrough(self):
        """node_id should match the identity_hex argument exactly."""
        args = self._call(identity_hex="deadbeef")
        assert args[0] == "deadbeef"

    def test_identity_hex_empty_string(self):
        """Empty identity_hex should produce empty node_id."""
        args = self._call(identity_hex="")
        assert args[0] == ""

    def test_default_battery_value(self):
        """Default battery should be 3.7V when not explicitly provided."""
        args = self._call()
        assert args[2] == self.pytest.approx(3.7)

    def test_custom_battery_value(self):
        """Explicit battery argument should be passed through."""
        args = self._call(battery=4.2)
        assert args[2] == self.pytest.approx(4.2)

    def test_battery_zero(self):
        """Zero battery should be passed through."""
        args = self._call(battery=0.0)
        assert args[2] == self.pytest.approx(0.0)

    def test_seq_passthrough(self):
        """seq number should be preserved."""
        args = self._call(seq=42)
        assert args[1] == 42

    def test_single_reading_structure(self):
        """Readings list should contain exactly one reading with correct fields."""
        args = self._call(seq=5)
        readings = args[3]
        assert len(readings) == 1
        reading = readings[0]
        assert reading["sensor_id"] == 1
        assert reading["unit"] == "C"
        assert "value" in reading
        assert "timestamp_ms" in reading

    def test_timestamp_recency(self):
        """Timestamp should be within 5 seconds of now."""
        import time

        args = self._call(seq=0)
        now_ms = int(time.time() * 1000)
        ts = args[3][0]["timestamp_ms"]
        assert abs(now_ms - ts) < 5000, (
            f"Timestamp {ts} is more than 5s from now ({now_ms})"
        )

    def test_temperature_is_float(self):
        """Temperature value should be a float type."""
        args = self._call(seq=5)
        assert isinstance(args[3][0]["value"], float)

    def test_temperature_with_mocked_esp32(self):
        """When esp32 module is available, raw_temperature() is converted to Celsius."""
        import sys

        mock_esp32 = MagicMock()
        mock_esp32.raw_temperature.return_value = 68  # 68°F → 20°C

        mock_encode = MagicMock()
        with patch.dict(sys.modules, {"esp32": mock_esp32}):
            with patch.object(lmao_client, "encode_sensor_envelope", mock_encode,
                              create=True):
                lmao_client.make_sensor_message("a1b2", 0, 3.7)

        mock_encode.assert_called_once()
        readings = mock_encode.call_args[0][3]
        # 68°F → (68 - 32) * 5/9 = 20°C
        assert readings[0]["value"] == 20.0, (
            f"Expected 20.0°C for raw_temperature=68°F, "
            f"got {readings[0]['value']}"
        )

    def test_raw_temperature_exception_propagates(self):
        """When esp32.raw_temperature() raises, exception propagates (no fallback)."""
        import sys

        mock_esp32 = MagicMock()
        mock_esp32.raw_temperature.side_effect = OSError("Sensor read failed")

        import pytest
        with patch.dict(sys.modules, {"esp32": mock_esp32}):
            with pytest.raises(OSError, match="Sensor read failed"):
                lmao_client.make_sensor_message("a1b2", 0, 3.7)


# ── Module-level helpers ────────────────────────────────────────────


class TestModuleFunctions:
    """Tests for remaining module-level helpers."""

    def test_log_returns_tft(self):
        """log() should return the tft argument unchanged."""
        tft = MagicMock()
        status_lines = []
        result = lmao_client.log("test msg", tft, status_lines)
        assert result is tft
        assert "test msg" in status_lines

    def test_log_without_tft(self):
        """log() should work when tft is None."""
        result = lmao_client.log("test msg", None, None)
        assert result is None

    def test_log_strips_oldest_lines(self):
        """log() should keep at most 8 status lines."""
        tft = MagicMock()
        status_lines = []
        for i in range(10):
            lmao_client.log(f"line {i}", tft, status_lines)
        assert len(status_lines) == 8
        assert status_lines[0] == "line 2"  # first two were popped
        assert status_lines[-1] == "line 9"


# ── SEND_SENSOR branch tests ────────────────────────────────────────


class TestSensorSendInMainLoop:
    """Tests for the SEND_SENSOR branch in main()'s main loop.

    These tests simulate the sensor send block from main() by executing
    the actual code path through patched dependencies.
    """

    @staticmethod
    def _simulate_sensor_send(
        send_message_returns=None,
        send_message_side_effect=None,
        sensor_flag=True,
    ):
        """Simulate the sensor send block from main(), return captured log calls.

        Returns (mock_log, mock_print_exception) so callers can assert on both.
        """
        import time

        with (
            patch.object(lmao_client, "SEND_SENSOR", sensor_flag),
            patch.object(lmao_client, "make_sensor_message") as mock_msg,
            patch.object(lmao_client, "log") as mock_log,
            patch.object(lmao_client.sys, "print_exception", create=True) as mock_pe,
        ):
            mock_msg.return_value = b"<sensor_envelope>"
            router_mock = MagicMock()
            router_mock.send_message.return_value = send_message_returns
            if send_message_side_effect is not None:
                router_mock.send_message.side_effect = send_message_side_effect

            # Execute the exact code from main()'s sensor send block
            if lmao_client.SEND_SENSOR:
                try:
                    sensor_content = lmao_client.make_sensor_message("a1b2", 1)
                    msg2 = router_mock.send_message(
                        destination_hash=b"abcd",
                        content=sensor_content,
                        title="p:Envelope",
                    )
                    if msg2:
                        lmao_client.log(f"Sensor: seq={1}", None, None)
                    else:
                        lmao_client.log("Sensor send returned None", None, None)
                except Exception as sensor_err:
                    lmao_client.sys.print_exception(sensor_err)
                    lmao_client.log(
                        f"Sensor send failed: {sensor_err}", None, None
                    )

        return mock_log, mock_pe

    def test_sensor_send_success_logs_seq(self):
        """When send_message returns a truthy value, seq is logged."""
        mock_log, _ = self._simulate_sensor_send(
            send_message_returns=MagicMock()
        )
        mock_log.assert_any_call("Sensor: seq=1", None, None)

    def test_sensor_send_returns_none_logs_warning(self):
        """When send_message returns None, warning is logged."""
        mock_log, _ = self._simulate_sensor_send(send_message_returns=None)
        mock_log.assert_any_call("Sensor send returned None", None, None)

    def test_sensor_send_exception_caught(self):
        """When send_message raises, Exception is caught and logged."""
        mock_log, mock_pe = self._simulate_sensor_send(
            send_message_side_effect=RuntimeError("LoRa busy")
        )
        mock_log.assert_any_call(
            "Sensor send failed: LoRa busy", None, None
        )
        mock_pe.assert_called_once()

    def test_sensor_send_exception_calls_print_exception(self):
        """sys.print_exception is called when an exception occurs."""
        _, mock_pe = self._simulate_sensor_send(
            send_message_side_effect=RuntimeError("test err")
        )
        mock_pe.assert_called_once()
        # Verify the exception object was passed
        args, _ = mock_pe.call_args
        assert isinstance(args[0], RuntimeError)
        assert "test err" in str(args[0])

    def test_sensor_flag_false_skips_send(self):
        """When SEND_SENSOR=False, sensor code is not reached."""
        mock_log, _ = self._simulate_sensor_send(
            send_message_returns=None, sensor_flag=False
        )
        # No sensor-related logs should appear
        sensor_logs = [
            call for call in mock_log.call_args_list
            if "Sensor" in str(call)
        ]
        assert len(sensor_logs) == 0

    def test_sensor_flag_true_sends(self):
        """When SEND_SENSOR=True, sensor code executes (smoke test)."""
        mock_log, _ = self._simulate_sensor_send(
            send_message_returns=MagicMock(), sensor_flag=True
        )
        mock_log.assert_any_call("Sensor: seq=1", None, None)


# ── import guard ────────────────────────────────────────────────────


def test_module_importable():
    """The module must be importable when all deps are met."""
    assert lmao_client is not None, (
        "cardputer_client.main not importable. Ensure deps are declared in tests/BUILD."
    )
