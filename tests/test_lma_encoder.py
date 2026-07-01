"""Test suite for lma_encoder — protobuf compatibility tests."""
import pytest
import sys
import os

# Add lma_encoder to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cardputer_client"))
from proto import lma_encoder as enc

# Reference protobuf-generated encoder (only available on server-side)
try:
    from lmao_server.proto import lma_pb2
    HAS_PROTOBUF = True
except ImportError:
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
        """Handle content near LoRa packet limit (~200 bytes)."""
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
        msg = lma_pb2.LMAOEnvelope()
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
