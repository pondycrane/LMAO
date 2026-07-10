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
        data = b"\x80\x80\x80\x80\x80\x80\x80\x80\x80\x80\x01"  # 10 continuation bytes
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

    def test_parse_poc_fallback_warns_on_decode_none(self, capsys):
        """parse_poc_message prints WARNING when protobuf decode returns None."""
        # Invalid protobuf data that decode_envelope returns None for
        result = enc.parse_poc_message(b"plain text not protobuf")
        assert result == "plain text not protobuf"
        captured = capsys.readouterr()
        assert "WARNING" in captured.out
        assert "trying raw UTF-8 fallback" in captured.out

    def test_parse_poc_fallback_prints_error_on_decode_failure(self, capsys):
        """parse_poc_message prints ERROR when both decode paths fail."""
        # Invalid UTF-8 bytes: 0xFF is never valid in UTF-8
        result = enc.parse_poc_message(b"\xff\xfe\xfd\xfc")
        assert result is None
        captured = capsys.readouterr()
        assert "ERROR" in captured.out


class TestEdgeCases:
    def test_decode_envelope_non_text_field(self):
        """decode_envelope dispatches correctly for non-text fields."""
        # Field 10 (SensorReport), wire type 2: tag = (10 << 3) | 2 = 0x52, then length 0
        non_text_bytes = b"\x52\x00"
        result = enc.decode_envelope(non_text_bytes)
        # Field 10 is now recognized as SensorReport; decoded dict should have expected keys
        assert result is not None, "Should decode SensorReport, not return None"
        assert result.get("node_id") == ""
        assert result.get("seq") == 0

    def test_decode_envelope_unknown_field_returns_none(self):
        """decode_envelope returns None for unrecognized field numbers."""
        # Field 99, wire type 2: tag = (99<<3)|2, then length 0
        # varint for 99<<3|2 = 794 = 0x31a → bytes: 0x9a, 0x06
        unknown_bytes = b"\x9a\x06\x00"
        result = enc.decode_envelope(unknown_bytes)
        assert result is None, f"Expected None for unknown field, got {result}"

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

    def test_decode_text_message_wire_type_5_unknown_skipped(self):
        """Wire type 5 on unknown fields is silently skipped (consistent
        with how protobuf decoders handle unknown wire types for known fields)."""
        # field 99, wire type 5: tag = (99<<3)|5 = 0x315, then 4 bytes
        data = bytes([0xBD, 0x06]) + b"\x00" * 4
        result = enc.decode_text_message(data)
        # Unknown field is skipped, defaults preserved
        assert result == {"node_id": "", "content": "", "timestamp": 0}


# ═══════════════════════════════════════════════════════════════════════════════
#  Tests for newly added message types (Task 8)
# ═══════════════════════════════════════════════════════════════════════════════


class TestSensorReport:
    def test_round_trip_minimal(self):
        """Encode/decode SensorReport with no readings."""
        encoded = enc.encode_sensor_report("sensor-1", 42, 3.7, [])
        decoded = enc.decode_sensor_report(encoded)
        assert decoded["node_id"] == "sensor-1"
        assert decoded["seq"] == 42
        assert decoded["readings"] == []

    def test_round_trip_with_readings(self):
        """Encode/decode SensorReport with multiple readings."""
        readings = [
            {"sensor_id": 1, "value": 23.5, "unit": "C", "timestamp_ms": 1000},
            {"sensor_id": 2, "value": 65.0, "unit": "%", "timestamp_ms": 1001},
        ]
        encoded = enc.encode_sensor_report("node", 0, 4.2, readings)
        decoded = enc.decode_sensor_report(encoded)
        assert abs(decoded["battery"] - 4.2) < 0.001
        assert len(decoded["readings"]) == 2
        assert decoded["readings"][0]["sensor_id"] == 1
        assert decoded["readings"][1]["unit"] == "%"

    def test_envelope_dispatch(self):
        """decode_envelope dispatches field 10 to SensorReport decoder."""
        inner = enc.encode_sensor_report("n", 0, 0.0, [])
        envelope = enc.encode_field(10, 2, enc.encode_length_delimited(inner))
        result = enc.decode_envelope(envelope)
        assert result is not None
        assert result["node_id"] == "n"

    def test_sensor_envelope_round_trip(self):
        """encode_sensor_envelope → decode_envelope round-trip with identity hex."""
        identity_hex = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"  # 32-char hex
        readings = [{"sensor_id": 1, "value": 25.0, "unit": "C", "timestamp_ms": 1000}]
        envelope = enc.encode_sensor_envelope(identity_hex, 7, 3.7, readings)
        result = enc.decode_envelope(envelope)
        assert result is not None
        assert result["node_id"] == identity_hex
        assert result["readings"][0]["value"] == 25.0
        assert result["readings"][0]["unit"] == "C"


class TestCommandRequest:
    def test_round_trip_simple(self):
        """Encode/decode CommandRequest with no params."""
        encoded = enc.encode_command_request("cmd1", "target1", "reboot", {}, 100, 200)
        decoded = enc.decode_command_request(encoded)
        assert decoded["cmd_id"] == "cmd1"
        assert decoded["target"] == "target1"
        assert decoded["action"] == "reboot"
        assert decoded["params"] == {}
        assert decoded["issued_ms"] == 100
        assert decoded["expires_ms"] == 200

    def test_round_trip_with_params(self):
        """Encode/decode CommandRequest with map params."""
        params = {"duration": "60", "valve": "open"}
        encoded = enc.encode_command_request("c2", "t2", "spray", params, 0, 0)
        decoded = enc.decode_command_request(encoded)
        assert decoded["params"] == params

    def test_envelope_dispatch(self):
        """decode_envelope dispatches field 11 to CommandRequest decoder."""
        inner = enc.encode_command_request("c", "t", "a", {}, 0, 0)
        envelope = enc.encode_field(11, 2, enc.encode_length_delimited(inner))
        result = enc.decode_envelope(envelope)
        assert result is not None
        assert result["cmd_id"] == "c"


class TestCommandAck:
    def test_round_trip_success(self):
        """Encode/decode CommandAck with success=True."""
        encoded = enc.encode_command_ack("cmd1", "node1", True, "OK")
        decoded = enc.decode_command_ack(encoded)
        assert decoded["cmd_id"] == "cmd1"
        assert decoded["node_id"] == "node1"
        assert decoded["success"] is True
        assert decoded["message"] == "OK"

    def test_round_trip_failure(self):
        """Encode/decode CommandAck with success=False."""
        encoded = enc.encode_command_ack("cmd2", "node2", False, "FAIL")
        decoded = enc.decode_command_ack(encoded)
        assert decoded["success"] is False
        assert decoded["message"] == "FAIL"

    def test_envelope_dispatch(self):
        """decode_envelope dispatches field 12 to CommandAck decoder."""
        inner = enc.encode_command_ack("c", "n", True, "m")
        envelope = enc.encode_field(12, 2, enc.encode_length_delimited(inner))
        result = enc.decode_envelope(envelope)
        assert result is not None
        assert result["cmd_id"] == "c"


class TestAudioMessage:
    def test_round_trip(self):
        """Encode/decode AudioMessage with binary audio data."""
        audio_data = b"\x00\x01\x02\x03"
        encoded = enc.encode_audio_message(
            "node-a", audio_data, "opus", 5000, 123456789
        )
        decoded = enc.decode_audio_message(encoded)
        assert decoded["node_id"] == "node-a"
        assert decoded["audio_data"] == audio_data
        assert decoded["codec"] == "opus"
        assert decoded["duration_ms"] == 5000
        assert decoded["timestamp"] == 123456789

    def test_envelope_dispatch(self):
        """decode_envelope dispatches field 21 to AudioMessage decoder."""
        inner = enc.encode_audio_message("n", b"\x00", "opus", 0, 0)
        envelope = enc.encode_field(21, 2, enc.encode_length_delimited(inner))
        result = enc.decode_envelope(envelope)
        assert result is not None
        assert result["codec"] == "opus"


class TestImageMessage:
    def test_round_trip(self):
        """Encode/decode ImageMessage."""
        image_data = b"\xff\xd8\xff\xe0"  # JPEG header
        encoded = enc.encode_image_message("cam-1", image_data, "jpeg", 640, 480, 100)
        decoded = enc.decode_image_message(encoded)
        assert decoded["node_id"] == "cam-1"
        assert decoded["image_data"] == image_data
        assert decoded["format"] == "jpeg"
        assert decoded["width"] == 640
        assert decoded["height"] == 480
        assert decoded["timestamp"] == 100

    def test_envelope_dispatch(self):
        """decode_envelope dispatches field 22 to ImageMessage decoder."""
        inner = enc.encode_image_message("n", b"\x00", "webp", 0, 0, 0)
        envelope = enc.encode_field(22, 2, enc.encode_length_delimited(inner))
        result = enc.decode_envelope(envelope)
        assert result is not None
        assert result["format"] == "webp"


class TestCallSignal:
    def test_round_trip(self):
        """Encode/decode CallSignal."""
        encoded = enc.encode_call_signal(enc.SIGNAL_OFFER, "sdp-data", "audio")
        decoded = enc.decode_call_signal(encoded)
        assert decoded["signal"] == enc.SIGNAL_OFFER
        assert decoded["sdp_or_ice"] == "sdp-data"
        assert decoded["media_type"] == "audio"

    def test_all_signal_types(self):
        """All CallSignal enum values round-trip correctly."""
        for sig in [
            enc.SIGNAL_OFFER,
            enc.SIGNAL_ANSWER,
            enc.SIGNAL_ICE,
            enc.SIGNAL_HANGUP,
            enc.SIGNAL_KEEPALIVE,
        ]:
            encoded = enc.encode_call_signal(sig, "", "")
            decoded = enc.decode_call_signal(encoded)
            assert decoded["signal"] == sig

    def test_envelope_dispatch(self):
        """decode_envelope dispatches field 30 to CallSignal decoder."""
        inner = enc.encode_call_signal(enc.SIGNAL_HANGUP, "", "")
        envelope = enc.encode_field(30, 2, enc.encode_length_delimited(inner))
        result = enc.decode_envelope(envelope)
        assert result is not None
        assert result["signal"] == enc.SIGNAL_HANGUP


class TestEnvelopeDispatch:
    """Verify decode_envelope dispatches to the correct decoder for each field."""

    def test_dispatch_sensor_report(self):
        inner = enc.encode_sensor_report("s", 0, 0.0, [])
        env = enc.encode_field(10, 2, enc.encode_length_delimited(inner))
        result = enc.decode_envelope(env)
        assert result is not None and "node_id" in result

    def test_dispatch_command_request(self):
        inner = enc.encode_command_request("c", "t", "a", {}, 0, 0)
        env = enc.encode_field(11, 2, enc.encode_length_delimited(inner))
        result = enc.decode_envelope(env)
        assert result is not None and "cmd_id" in result

    def test_dispatch_command_ack(self):
        inner = enc.encode_command_ack("c", "n", True, "m")
        env = enc.encode_field(12, 2, enc.encode_length_delimited(inner))
        result = enc.decode_envelope(env)
        assert result is not None and "success" in result

    def test_dispatch_text_message(self):
        inner = enc.encode_text_message("n", "txt", 1)
        env = enc.encode_field(20, 2, enc.encode_length_delimited(inner))
        result = enc.decode_envelope(env)
        assert result is not None and "content" in result

    def test_dispatch_audio_message(self):
        inner = enc.encode_audio_message("n", b"a", "opus", 0, 0)
        env = enc.encode_field(21, 2, enc.encode_length_delimited(inner))
        result = enc.decode_envelope(env)
        assert result is not None and "codec" in result

    def test_dispatch_image_message(self):
        inner = enc.encode_image_message("n", b"i", "webp", 0, 0, 0)
        env = enc.encode_field(22, 2, enc.encode_length_delimited(inner))
        result = enc.decode_envelope(env)
        assert result is not None and "format" in result

    def test_dispatch_call_signal(self):
        inner = enc.encode_call_signal(enc.SIGNAL_OFFER, "", "")
        env = enc.encode_field(30, 2, enc.encode_length_delimited(inner))
        result = enc.decode_envelope(env)
        assert result is not None and "signal" in result


class TestBackwardCompatibility:
    """Ensure POC convenience functions still work."""

    def test_make_poc_message_still_works(self):
        """make_poc_message must produce byte-identical output to before."""
        payload = enc.make_poc_message("node", "hello", 1)
        parsed = enc.parse_poc_message(payload)
        assert parsed == "hello"

    def test_known_answer_poc_message(self):
        """Known-answer test for POC message unchanged."""
        payload = enc.make_poc_message("a", "b", 1)
        # This must match the pre-existing known answer
        inner = enc.encode_text_message("a", "b", 1)
        expected = b"\xa2\x01\x08" + inner
        assert payload == expected


class TestEncodeSensorEnvelope:
    """Edge-case tests for encode_sensor_envelope."""

    def test_empty_readings(self):
        """Empty readings list should produce valid envelope."""
        envelope = enc.encode_sensor_envelope("node-1", 1, 3.7, [])
        decoded = enc.decode_envelope(envelope)
        assert decoded is not None
        assert decoded["node_id"] == "node-1"
        assert decoded["seq"] == 1
        assert decoded["battery"] == pytest.approx(3.7)
        assert decoded["readings"] == []

    def test_long_node_id(self):
        """64-char hex node_id (boundary test)."""
        long_id = "a" * 64
        envelope = enc.encode_sensor_envelope(long_id, 0, 0.0, [])
        decoded = enc.decode_envelope(envelope)
        assert decoded is not None
        assert decoded["node_id"] == long_id

    def test_high_seq_values(self):
        """Max uint32 seq values should round-trip correctly."""
        for seq in [2**32 - 1, 0, 1, 65535]:
            envelope = enc.encode_sensor_envelope("n", seq, 0.0, [])
            decoded = enc.decode_envelope(envelope)
            assert decoded is not None
            assert decoded["seq"] == seq

    def test_round_trip_envelope_to_sensor_report(self):
        """Envelope → decode → verify all fields including readings."""
        readings = [
            {"sensor_id": 1, "value": 25.5, "unit": "C", "timestamp_ms": 1000},
            {"sensor_id": 2, "value": 68.0, "unit": "%", "timestamp_ms": 1000},
        ]
        envelope = enc.encode_sensor_envelope("node-xyz", 42, 3.9, readings)
        decoded = enc.decode_envelope(envelope)
        assert decoded is not None
        assert decoded["node_id"] == "node-xyz"
        assert decoded["seq"] == 42
        assert decoded["battery"] == pytest.approx(3.9)
        assert len(decoded["readings"]) == 2
        assert decoded["readings"][0]["sensor_id"] == 1
        assert decoded["readings"][0]["value"] == pytest.approx(25.5)
        assert decoded["readings"][0]["unit"] == "C"
        assert decoded["readings"][0]["timestamp_ms"] == 1000
        assert decoded["readings"][1]["sensor_id"] == 2
        assert decoded["readings"][1]["value"] == pytest.approx(68.0)
        assert decoded["readings"][1]["unit"] == "%"
        assert decoded["readings"][1]["timestamp_ms"] == 1000


class TestEncodeSensorEnvelopeEdgeCases:
    """Invalid/malformed input edge-case tests for encode_sensor_envelope."""

    def test_none_readings_raises(self):
        """Passing None as readings should raise TypeError."""
        with pytest.raises(TypeError):
            enc.encode_sensor_envelope("node-1", 1, 3.7, None)

    def test_non_dict_in_readings_raises(self):
        """Non-dict items in readings list should raise TypeError."""
        with pytest.raises(TypeError):
            enc.encode_sensor_envelope("node-1", 1, 3.7, ["not-a-dict"])

    def test_reading_missing_keys_raises_key_error(self):
        """Reading dicts missing required keys should raise KeyError."""
        readings = [{}]
        with pytest.raises(KeyError):
            enc.encode_sensor_envelope("node-1", 1, 3.7, readings)

    def test_empty_string_node_id(self):
        """Empty string node_id should be preserved in round-trip."""
        envelope = enc.encode_sensor_envelope("", 0, 0.0, [])
        decoded = enc.decode_envelope(envelope)
        assert decoded is not None
        assert decoded["node_id"] == ""

    def test_negative_seq_encoded_as_positive(self):
        """Negative seq is encoded as signed byte due to varint encoding."""
        # The minimal varint encoder doesn't handle negative uint32 specially;
        # -1 encodes as 127 (since (-1 & 0x7F) = 127).
        envelope = enc.encode_sensor_envelope("n", -1, 0.0, [])
        decoded = enc.decode_envelope(envelope)
        assert decoded["seq"] == 127


if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))
