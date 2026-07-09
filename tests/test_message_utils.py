"""Direct unit tests for lma_core.message_utils.decode_lmao_message.

Covers all five code paths of the shared utility function used by
both the server and human client.
"""
from unittest.mock import MagicMock, patch

from google.protobuf.message import DecodeError

from lma_core.message_utils import decode_lmao_message


class TestDecodeLMAOMessage:
    """Unit tests for decode_lmao_message() — pure function, no conftest needed."""

    def test_empty_bytes_returns_empty_string(self):
        """Empty bytes should return ''."""
        assert decode_lmao_message(b"") == ""

    def test_protobuf_text_content(self):
        """When protobuf ParseFromString succeeds and HasField('text') is True,
        returns text.content."""
        mock_envelope = MagicMock()
        mock_envelope.HasField.return_value = True
        mock_envelope.text.content = "Hello from protobuf"

        with patch("lma_core.LMAOEnvelope", return_value=mock_envelope):
            result = decode_lmao_message(b"some-bytes")
            assert result == "Hello from protobuf"

    def test_protobuf_non_text_falls_through(self):
        """When protobuf succeeds but HasField('text') is False,
        falls through to UTF-8 fallback."""
        mock_envelope = MagicMock()
        mock_envelope.HasField.return_value = False

        with patch("lma_core.LMAOEnvelope", return_value=mock_envelope):
            result = decode_lmao_message(b"raw text")
            assert result == "raw text"

    def test_protobuf_decode_error_falls_back_to_utf8(self):
        """When protobuf ParseFromString raises DecodeError,
        falls back to UTF-8 decoding."""
        mock_envelope = MagicMock()
        mock_envelope.ParseFromString.side_effect = DecodeError("bad proto")

        with patch("lma_core.LMAOEnvelope", return_value=mock_envelope):
            result = decode_lmao_message(b"hello world")
            assert result == "hello world"

    def test_binary_content_returns_placeholder(self):
        """When both protobuf and UTF-8 decoding fail,
        returns byte-count placeholder."""
        mock_envelope = MagicMock()
        mock_envelope.ParseFromString.side_effect = DecodeError("bad proto")

        with patch("lma_core.LMAOEnvelope", return_value=mock_envelope):
            result = decode_lmao_message(b"\xff\xfe\x00\x01")
            assert "<non-text: 4 bytes>" in result
