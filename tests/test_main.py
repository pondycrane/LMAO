"""Unit tests for cardputer_client.main — no hardware required.

Tests the pure-logic helpers that can be exercised without a Cardputer device:
  - _needs_wifi()
  - pending_replies drain (via mock patch)

The main() boot sequence itself requires MicroPython-only modules (Reticulum,
LXMRouter, machine, st7789, network) and is tested via E2E tests only.

Run with::

    bazel test //tests:test_main --test_output=all
"""

from unittest.mock import MagicMock


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

        # Should not raise
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


# ── import guard ────────────────────────────────────────────────────

def test_module_importable():
    """The module must be importable when all deps are met."""
    assert lmao_client is not None, (
        "cardputer_client.main not importable. "
        "Ensure deps are declared in tests/BUILD."
    )
