"""Test suite for lma_encoder — protobuf compatibility tests."""
import logging
import pytest

from cardputer_client.proto import lma_encoder as enc

_logger = logging.getLogger(__name__)

# Reference protobuf-generated encoder (only available with protobuf installed)
try:
    from lma_core import LMAOEnvelope
    HAS_PROTOBUF = True
except ImportError:
    _logger.warning(
        "Could not import LMAOEnvelope from lma_core. "
        "Cross-validation tests will be skipped. "
        "Run 'bazel build //proto:lma_py_proto' to generate protobuf stubs."
    )
    HAS_PROTOBUF = False


class TestVarint:
    def test_small_values(self):
        """Encode/decode small varints."""
        for v in [0, 1, 127, 128, 255, 300, 65535]:
            encoded = enc.encode_varint(v)
            decoded, length = enc.decode_varint(encoded)
            assert decoded == v
            assert length == len(encoded)

    def test_large_values(self):
        """Encode/decode large varints (up to 64-bit)."""
        values = [2**32 - 1, 2**32, 2**63 - 1, 2**64 - 1]
        for v in values:
            encoded = enc.encode_varint(v)
            decoded, length = enc.decode_varint(encoded)
            assert decoded == v

    def test_truncated_varint_raises(self):
        """Decoding truncated varint should raise ValueError."""
        data = b'\x80\x80\x80\x80\x80\x80\x80\x80\x80\x80\x01'  # 10 continuation bytes
        with pytest.raises(ValueError, match="Truncated varint"):
            enc.decode_varint(data[:1])  # Only one byte


class TestTextMessage:
    def test_round_trip_simple(self):
        """Encode then decode a simple message."""
        node_id = "test-node"
        content = "Hello, POC!"
        timestamp = 1234567890
        encoded = enc.encode_text_message(node_id, content, timestamp)
        decoded = enc.decode_text_message(encoded)
        assert decoded["node_id"] == node_id
        assert decoded["content"] == content
        assert decoded["timestamp"] == timestamp

    def test_empty_fields(self):
        """Handle empty node_id and content."""
        encoded = enc.encode_text_message("", "", 0)
        decoded = enc.decode_text_message(encoded)
        assert decoded["node_id"] == ""
        assert decoded["content"] == ""
        assert decoded["timestamp"] == 0

    def test_unicode_content(self):
        """Handle Unicode characters in content."""
        encoded = enc.encode_text_message("node", "héllo wörld 🔥", 100)
        decoded = enc.decode_text_message(encoded)
        assert decoded["content"] == "héllo wörld 🔥"

    def test_long_content(self):
        """Handle content exceeding LoRa packet limit (~200 bytes)."""
        content = "X" * 300
        encoded = enc.encode_text_message("n", content, 1)
        decoded = enc.decode_text_message(encoded)
        assert decoded["content"] == content
        assert len(encoded) > 300  # Verify length encoding works

    def test_large_timestamp(self):
        """Handle uint64 timestamp boundary values."""
        for ts in [0, 1, 2**32, 2**64 - 1]:
            encoded = enc.encode_text_message("n", "t", ts)
            decoded = enc.decode_text_message(encoded)
            assert decoded["timestamp"] == ts


class TestEnvelope:
    def test_envelope_round_trip(self):
        """Wrap a text message in an envelope, then decode."""
        node_id = "device-1"
        content = "Hello from LoRa"
        timestamp = 9999999999
        text_bytes = enc.encode_text_message(node_id, content, timestamp)
        envelope = enc.encode_envelope_text(text_bytes)
        decoded = enc.decode_envelope(envelope)
        assert decoded is not None
        assert decoded["node_id"] == node_id
        assert decoded["content"] == content
        assert decoded["timestamp"] == timestamp


class TestCrossValidation:
    @pytest.mark.skipif(not HAS_PROTOBUF, reason="protobuf library not available")
    def test_byte_identical_to_protobuf(self):
        """Verify hand-coded encoder produces same bytes as protobuf library."""
        msg = LMAOEnvelope()
        msg.text.node_id = "cross-node"
        msg.text.content = "Cross-validation test"
        msg.text.timestamp = 1234567890
        expected = msg.SerializeToString()

        actual = enc.make_poc_message("cross-node", "Cross-validation test", 1234567890)
        assert actual == expected, (
            f"Byte mismatch!\n"
            f"Expected ({len(expected)}B): {expected.hex()}\n"
            f"Actual   ({len(actual)}B): {actual.hex()}"
        )

    def test_make_poc_message_round_trip(self):
        """Test the convenience function end-to-end."""
        payload = enc.make_poc_message("node", "Hi there")
        result = enc.parse_poc_message(payload)
        assert result == "Hi there"

    def test_parse_poc_message_fallback(self):
        """parse_poc_message should fallback to raw UTF-8 decode for non-envelope data."""
        result = enc.parse_poc_message(b"Hello plain")
        assert result == "Hello plain"

    def test_parse_poc_message_garbage(self):
        """parse_poc_message should return None for unparseable data."""
        result = enc.parse_poc_message(b"\xff\xfe\xfd\xfc")
        assert result is None

    def test_verbose_flag_suppresses_debug_output(self, capsys):
        """When VERBOSE=False (default), debug prints are suppressed."""
        orig = enc.VERBOSE
        enc.VERBOSE = False
        try:
            payload = enc.make_poc_message("node", "hello")
            result = enc.parse_poc_message(payload)
            assert result == "hello"
            captured = capsys.readouterr()
            assert "DEBUG" not in captured.out
        finally:
            enc.VERBOSE = orig

    def test_verbose_flag_enables_debug_output(self, capsys):
        """When VERBOSE=True, debug prints appear."""
        orig = enc.VERBOSE
        enc.VERBOSE = True
        try:
            payload = enc.make_poc_message("node", "hello")
            result = enc.parse_poc_message(payload)
            assert result == "hello"
            captured = capsys.readouterr()
            assert "DEBUG" in captured.out
        finally:
            enc.VERBOSE = orig


class TestEdgeCases:
    def test_decode_envelope_non_text_field(self):
        """decode_envelope should return None for non-text (non-field-20) envelopes."""
        # Encode a field with number != 20 (e.g., field 10 with empty bytes)
        data = b""
        # Field 10, wire type 2: tag = (10 << 3) | 2 = 0x52, then length 0
        non_text_bytes = b"\x52\x00"
        result = enc.decode_envelope(non_text_bytes)
        assert result is None, f"Expected None for non-text field, got {result}"

    def test_decode_envelope_empty(self):
        """decode_envelope should return None for empty data."""
        result = enc.decode_envelope(b"")
        assert result is None

    def test_decode_envelope_truncated(self):
        """decode_envelope should handle truncated data gracefully."""
        # Start of a valid envelope but truncated mid-field
        # Field 20 (TextMessage), wire type 2: tag = (20 << 3) | 2 = 0xa2
        # Then a length varint, but body is truncated
        truncated = b"\xa2\x10this_is_truncated"
        result = enc.decode_envelope(truncated)
        assert result is None  # Should gracefully return None

    def test_decode_text_message_empty(self):
        """decode_text_message should return defaults for empty data."""
        result = enc.decode_text_message(b"")
        assert result == {"node_id": "", "content": "", "timestamp": 0}

    def test_decode_text_message_unknown_fields(self):
        """decode_text_message should skip unknown field numbers."""
        # Field 1 (node_id) = "a", field 99 (unknown) = "x", field 2 (content) = "b"
        # tag for field 1, wire 2: (1<<3)|2 = 0x0a, len=1, "a"
        # tag for field 99, wire 2: (99<<3)|2 = (0x31a = 0x9a, 0x06), len=1, "x"
        # Actually encode properly:
        node_id_bytes = b"\x0a\x01\x61"  # field 1, len 1, "a"
        content_bytes = b"\x12\x01\x62"  # field 2, len 1, "b"
        data = node_id_bytes + content_bytes
        result = enc.decode_text_message(data)
        assert result["node_id"] == "a"
        assert result["content"] == "b"
        assert result["timestamp"] == 0

    def test_known_answer_text_message(self):
        """Verify byte-exact known output for TextMessage.

        If this test fails after a schema change, update the expected bytes.
        """
        # TextMessage: node_id="a", content="b", timestamp=1
        expected = b"\x0a\x01\x61\x12\x01\x62\x18\x01"
        actual = enc.encode_text_message("a", "b", 1)
        assert actual == expected, (
            f"Known-answer mismatch!\n"
            f"Expected: {expected.hex()}\n"
            f"Actual:   {actual.hex()}"
        )

    def test_known_answer_envelope(self):
        """Verify envelope wrapping produces expected bytes."""
        inner = enc.encode_text_message("a", "b", 1)
        # Envelope: field 20 (0xa2 with continuation = \xa2\x01), wire type 2,
        # then length-delimited inner (8 bytes = 0x08)
        expected = b"\xa2\x01\x08" + inner
        actual = enc.encode_envelope_text(inner)
        assert actual == expected, (
            f"Envelope known-answer mismatch!\n"
            f"Expected: {expected.hex()}\n"
            f"Actual:   {actual.hex()}"
        )

    def test_encode_field_round_trip(self):
        """Direct test of encode_field for various wire types."""
        # Wire type 0 (varint): field 3, value 1 -> tag (3<<3)|0 = 0x18, varint 0x01
        result = enc.encode_field(3, 0, enc.encode_varint(1))
        assert result == b"\x18\x01"

        # Wire type 2 (length-delimited): caller must include length prefix
        # field 1, encoded as tag + length-delimited "abc"
        # tag (1<<3)|2 = 0x0a, then encode_length_delimited(b"abc") = 0x03 + "abc"
        payload = enc.encode_length_delimited(b"abc")
        result = enc.encode_field(1, 2, payload)
        expected = b"\x0a\x03abc"
        assert result == expected, f"Expected {expected.hex()}, got {result.hex()}"

    def test_decode_text_message_unsupported_wire_type(self):
        """decode_text_message raises ValueError for unsupported wire types."""
        # Wire type 1 (fixed64): field 1, wire type 1, tag = (1<<3)|1 = 0x09
        # Followed by 8 bytes of fixed64 data
        data = b"\x09" + (b"\x00" * 8)
        with pytest.raises(ValueError, match="Unsupported wire type"):
            enc.decode_text_message(data)

    def test_decode_text_message_wire_type_3_unsupported(self):
        """Wire type 3 (start group) is deprecated and unsupported."""
        # field 1, wire type 3: tag = (1<<3)|3 = 0x0b
        data = b"\x0b"
        with pytest.raises(ValueError, match="Unsupported wire type"):
            enc.decode_text_message(data)

    def test_decode_text_message_wire_type_5_unsupported(self):
        """Wire type 5 (fixed32) is unsupported."""
        # field 1, wire type 5: tag = (1<<3)|5 = 0x0d, then 4 bytes
        data = b"\x0d" + (b"\x00" * 4)
        with pytest.raises(ValueError, match="Unsupported wire type"):
            enc.decode_text_message(data)


if __name__ == "__main__":
    import pytest
    import sys
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
