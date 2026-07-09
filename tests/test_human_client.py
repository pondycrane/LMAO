"""Tests for human client message handler (with mocked RNS/LXMF)."""
from unittest.mock import MagicMock, patch
import pytest
import sys
from google.protobuf.message import DecodeError

from conftest import setup_common_mocks, cleanup_common_mocks


@pytest.fixture
def client_with_mocks():
    """Set up mocks for RNS, LXMF, and create a Client instance.

    Replaces the real RNS/LXMF modules with mocks so we can test
    handle_lxmf_delivery without real Reticulum hardware.
    Returns a Client instance with router and client_identity set.
    """
    # Force reload of client module to get fresh state
    if "client" in sys.modules:
        del sys.modules["client"]

    setup_common_mocks(with_grpc=False)

    # Configure mock envelope so protobuf decode raises DecodeError (triggering fallback)
    mock_envelope = MagicMock()
    mock_envelope.ParseFromString.side_effect = DecodeError("Test decode error")
    mock_envelope.SerializeToString.return_value = b"mock-serialized-envelope"
    sys.modules["lma_core"].LMAOEnvelope.return_value = mock_envelope

    from human_client import client

    client_instance = client.Client()
    client_instance.router = MagicMock()
    client_instance.client_identity = MagicMock()
    client_instance.client_identity.hash = b"\x01" * 16

    yield client_instance

    cleanup_common_mocks()


class TestHandleLXMFDelivery:
    """Tests for client message handling (receive path)."""

    def test_message_displayed_for_valid_message(self, client_with_mocks, capsys):
        """Handle valid message and verify it is displayed to terminal."""
        client = client_with_mocks

        msg = MagicMock()
        msg.get_source.return_value = MagicMock()
        msg.get_source.return_value.hash = b"\x02" * 16
        msg.content = b"Hello from test"
        msg.title_as_string.return_value = "p:Envelope"

        client.handle_lxmf_delivery(msg)

        # Message should be printed to stdout
        captured = capsys.readouterr()
        assert "MSG from" in captured.out, "Should print incoming message prefix"
        assert "Hello from test" in captured.out, "Should display message content"

        # No reply should be sent (unlike server)
        client.router.handle_outbound.assert_not_called()

    def test_no_reply_sent_for_valid_message(self, client_with_mocks):
        """Client should NOT auto-respond (unlike server which sends ACK)."""
        client = client_with_mocks

        msg = MagicMock()
        msg.get_source.return_value = MagicMock()
        msg.get_source.return_value.hash = b"\x02" * 16
        msg.content = b"Hello from test"
        msg.title_as_string.return_value = "p:Envelope"

        client.handle_lxmf_delivery(msg)

        # No reply — human client does not auto-ACK
        client.router.handle_outbound.assert_not_called()

    def test_handles_message_without_source(self, client_with_mocks):
        """Handle message with no source identity gracefully."""
        client = client_with_mocks

        msg = MagicMock()
        msg.get_source.return_value = None
        msg.content = b"anonymous"

        # Should not crash
        client.handle_lxmf_delivery(msg)
        client.router.handle_outbound.assert_not_called()

    def test_handles_message_without_router(self, client_with_mocks):
        """Handle message when router is None."""
        client = client_with_mocks
        client.router = None

        msg = MagicMock()
        msg.get_source.return_value = MagicMock()
        msg.content = b"test"

        # Should not crash, should not send
        client.handle_lxmf_delivery(msg)

    def test_handles_empty_content(self, client_with_mocks):
        """Handle message with empty content."""
        client = client_with_mocks

        msg = MagicMock()
        msg.get_source.return_value = MagicMock()
        msg.get_source.return_value.hash = b"\x03" * 16
        msg.content = b""
        msg.title_as_string.return_value = "p:Envelope"

        client.handle_lxmf_delivery(msg)
        # Should not crash and not send reply
        client.router.handle_outbound.assert_not_called()

    def test_handles_missing_content(self, client_with_mocks):
        """Handler should not crash on messages missing 'content' attribute."""
        client = client_with_mocks

        msg = MagicMock()
        msg.get_source.return_value = None
        # Remove content to simulate missing attribute
        del msg.content

        # Should not raise
        client.handle_lxmf_delivery(msg)
        client.router.handle_outbound.assert_not_called()

    def test_handles_attribute_error(self, client_with_mocks):
        """Handler should not crash when get_source raises AttributeError."""
        client = client_with_mocks

        msg = MagicMock()
        msg.get_source.side_effect = AttributeError("No source")

        # Should not raise
        client.handle_lxmf_delivery(msg)
        client.router.handle_outbound.assert_not_called()

    def test_protobuf_decode_success_path(self, client_with_mocks, capsys):
        """Verify protobuf-decoded content is displayed when ParseFromString succeeds."""
        client = client_with_mocks

        # Reconfigure mock to simulate successful protobuf decode
        mock_envelope = MagicMock()
        mock_envelope.HasField.return_value = True
        mock_envelope.text.content = "Hello from protobuf"
        sys.modules["lma_core"].LMAOEnvelope.return_value = mock_envelope

        msg = MagicMock()
        msg.get_source.return_value = MagicMock()
        msg.get_source.return_value.hash = b"\x04" * 16
        msg.content = b"protobuf-bytes"
        msg.title_as_string.return_value = "p:Envelope"

        client.handle_lxmf_delivery(msg)

        captured = capsys.readouterr()
        assert "MSG from" in captured.out
        assert "Hello from protobuf" in captured.out

    def test_protobuf_decode_non_text_field(self, client_with_mocks):
        """Verify fallback when protobuf succeeds but has no text field."""
        client = client_with_mocks

        # Reconfigure mock: ParseFromString succeeds but HasField('text') is False
        mock_envelope = MagicMock()
        mock_envelope.HasField.return_value = False
        sys.modules["lma_core"].LMAOEnvelope.return_value = mock_envelope

        msg = MagicMock()
        msg.get_source.return_value = MagicMock()
        msg.get_source.return_value.hash = b"\x05" * 16
        msg.content = b"non-text protobuf bytes"
        msg.title_as_string.return_value = "p:Envelope"

        client.handle_lxmf_delivery(msg)
        # Should not crash
        client.router.handle_outbound.assert_not_called()

    def test_handles_binary_content(self, client_with_mocks, capsys):
        """Handler should not crash on binary non-decodable content."""
        client = client_with_mocks

        msg = MagicMock()
        msg.get_source.return_value = MagicMock()
        msg.get_source.return_value.hash = b"\x06" * 16
        msg.content = b"\xff\xfe\xfd\xfc\x00"
        msg.title_as_string.return_value = "p:Envelope"

        client.handle_lxmf_delivery(msg)
        # Should not crash
        captured = capsys.readouterr()
        assert "MSG from" in captured.out
        assert "non-text" in captured.out or "bytes" in captured.out

    def test_protobuf_decode_uses_content_from_text_field(
        self, client_with_mocks, capsys
    ):
        """When protobuf decode succeeds and HasField('text') is True,
        the content from text.content is displayed."""
        client = client_with_mocks

        # Reconfigure mock to simulate successful protobuf decode
        mock_envelope = MagicMock()
        mock_envelope.HasField.return_value = True
        mock_envelope.text.content = "Decoded protobuf text"
        sys.modules["lma_core"].LMAOEnvelope.return_value = mock_envelope

        msg = MagicMock()
        msg.get_source.return_value = MagicMock()
        msg.get_source.return_value.hash = b"\x07" * 16
        msg.content = b"protobuf-encoded-bytes"
        msg.title_as_string.return_value = "p:Envelope"

        client.handle_lxmf_delivery(msg)

        captured = capsys.readouterr()
        assert "Decoded protobuf text" in captured.out

    def test_protobuf_decode_non_text_uses_fallback(self, client_with_mocks, capsys):
        """When protobuf decode succeeds but HasField('text') is False,
        the handler falls back to raw UTF-8 decode of content bytes."""
        client = client_with_mocks

        # Reconfigure mock: ParseFromString succeeds but HasField('text') is False
        mock_envelope = MagicMock()
        mock_envelope.HasField.return_value = False
        sys.modules["lma_core"].LMAOEnvelope.return_value = mock_envelope

        msg = MagicMock()
        msg.get_source.return_value = MagicMock()
        msg.get_source.return_value.hash = b"\x08" * 16
        msg.content = b"plain text fallback"
        msg.title_as_string.return_value = "p:Envelope"

        client.handle_lxmf_delivery(msg)

        captured = capsys.readouterr()
        assert "MSG from" in captured.out
        assert "plain text fallback" in captured.out

    def test_protobuf_decode_binary_invalid_utf8(self, client_with_mocks, capsys):
        """When protobuf decode fails and content is not valid UTF-8,
        the handler shows a byte-count placeholder instead."""
        client = client_with_mocks

        # Fixture already has ParseFromString side_effect = DecodeError
        msg = MagicMock()
        msg.get_source.return_value = MagicMock()
        msg.get_source.return_value.hash = b"\x09" * 16
        msg.content = b"\xff\xfe\x00\x01"  # intentionally invalid UTF-8
        msg.title_as_string.return_value = "p:Envelope"

        # Handler should not crash
        client.handle_lxmf_delivery(msg)
        captured = capsys.readouterr()
        assert "MSG from" in captured.out
        assert "non-text" in captured.out

    def test_handles_rns_exception(self, client_with_mocks):
        """Handler should not crash when message triggers RNSException during processing."""
        client = client_with_mocks

        RNSException = sys.modules["RNS"].RNSException

        msg = MagicMock()
        # get_source raises RNSException (e.g., corrupted identity)
        msg.get_source.side_effect = RNSException("Invalid identity")
        msg.content = b"test"

        # Should not raise
        client.handle_lxmf_delivery(msg)
        client.router.handle_outbound.assert_not_called()

    def test_handles_generic_exception(self, client_with_mocks):
        """Handler should not crash on unexpected exceptions during message processing."""
        client = client_with_mocks

        msg = MagicMock()
        # Raise a completely unexpected exception
        msg.get_source.side_effect = RuntimeError("Unexpected internal error")

        # Should not raise
        client.handle_lxmf_delivery(msg)
        client.router.handle_outbound.assert_not_called()


VALID_HASH = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"  # 32 hex characters


class TestParseInput:
    """Tests for Client._parse_input REPL command dispatch."""

    def test_quit_returns_false(self, client_with_mocks):
        """/quit should cause the REPL to exit."""
        client = client_with_mocks
        result = client._parse_input("/quit")
        assert result is False

    def test_exit_returns_false(self, client_with_mocks):
        """/exit should cause the REPL to exit."""
        client = client_with_mocks
        result = client._parse_input("/exit")
        assert result is False

    def test_empty_input_returns_true(self, client_with_mocks):
        """Empty input should keep the REPL running."""
        client = client_with_mocks
        result = client._parse_input("")
        assert result is True

    def test_whitespace_only_returns_true(self, client_with_mocks):
        """Whitespace-only input should keep the REPL running."""
        client = client_with_mocks
        result = client._parse_input("   ")
        assert result is True

    def test_help_prints_and_returns_true(self, client_with_mocks, capsys):
        """/help should print help text and keep the REPL running."""
        client = client_with_mocks
        result = client._parse_input("/help")
        assert result is True
        captured = capsys.readouterr()
        assert "Available commands" in captured.out

    def test_dest_sets_hash(self, client_with_mocks):
        """/dest with a valid hash should set the default destination."""
        client = client_with_mocks
        result = client._parse_input(f"/dest {VALID_HASH}")
        assert result is True
        assert client._default_dest_hash == VALID_HASH

    def test_dest_rejects_invalid_hash(self, client_with_mocks, capsys):
        """/dest with an invalid hash should print an error."""
        client = client_with_mocks
        result = client._parse_input("/dest short")
        assert result is True
        captured = capsys.readouterr()
        assert "Invalid hash" in captured.out

    def test_dest_missing_arg_shows_usage(self, client_with_mocks, capsys):
        """/dest without an argument should show usage."""
        client = client_with_mocks
        result = client._parse_input("/dest")
        assert result is True
        captured = capsys.readouterr()
        assert "Usage" in captured.out

    def test_send_with_dest_and_msg(self, client_with_mocks):
        """/send with valid hash and message should call _send_message."""
        client = client_with_mocks
        with patch.object(client, "_send_message", return_value=True) as mock_send:
            result = client._parse_input(f"/send {VALID_HASH} Hello there")
            assert result is True
            mock_send.assert_called_once()

    def test_send_missing_args_shows_usage(self, client_with_mocks, capsys):
        """/send without enough arguments should show usage."""
        client = client_with_mocks
        result = client._parse_input("/send")
        assert result is True
        captured = capsys.readouterr()
        assert "Usage" in captured.out

    def test_send_invalid_hash_prints_error(self, client_with_mocks, capsys):
        """/send with invalid hash should print error."""
        client = client_with_mocks
        result = client._parse_input("/send short message")
        assert result is True
        captured = capsys.readouterr()
        assert "Invalid hash" in captured.out

    def test_unknown_command_falls_to_plain_text(self, client_with_mocks, capsys):
        """Unknown commands are treated as plain text (no dispatch prefix).

        Since no default destination is set, REPL prints a hint and continues.
        """
        client = client_with_mocks
        result = client._parse_input("/unknown")
        assert result is True
        captured = capsys.readouterr()
        assert "No default destination" in captured.out

    def test_plain_text_no_default_prints_hint(self, client_with_mocks, capsys):
        """Plain text without default destination should print a hint."""
        client = client_with_mocks
        client._default_dest_hash = None
        result = client._parse_input("Hello there")
        assert result is True
        captured = capsys.readouterr()
        assert "No default destination" in captured.out

    def test_plain_text_with_default_sends(self, client_with_mocks):
        """Plain text with default destination should send the message."""
        client = client_with_mocks
        client._default_dest_hash = VALID_HASH
        client._default_dest_identity = MagicMock()
        with patch.object(client, "_send_message", return_value=True) as mock_send:
            result = client._parse_input("Hello there")
            assert result is True
            mock_send.assert_called_once()


class TestValidateHash:
    """Tests for Client._validate_hash static method."""

    @pytest.fixture
    def client_class(self):
        """Import Client after setting up mocks for the human_client module."""
        setup_common_mocks(with_grpc=False)
        from human_client.client import Client

        yield Client
        cleanup_common_mocks()

    @pytest.mark.parametrize("hash_str,expected_valid", [
        ("a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4", True),
        ("00000000000000000000000000000000", True),
        ("FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF", True),
        ("", False),
        ("abc", False),  # Too short
        ("not-hex!!", False),  # Invalid hex
        ("a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5", False),  # 31 chars — hex but wrong length
        ("a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e", False),  # 31 chars — hex but wrong length
        ("g1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4", False),  # 'g' not hex
        ("a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6!", False),  # 33 chars — hex but wrong length
    ])
    def test_validates_hash(self, client_class, hash_str, expected_valid):
        """Verify hash validation for various inputs."""
        valid, err = client_class._validate_hash(hash_str)

        # For invalid hashes, we need to check various failure reasons
        if expected_valid:
            assert valid, f"Expected valid=True for {hash_str!r}, got False: {err}"
        else:
            assert not valid, f"Expected valid=False for {hash_str!r}, got True: {err}"


class TestSendMessage:
    """Tests for client message sending."""

    def test_send_message_constructs_lxmf_correctly(self, client_with_mocks):
        """Verify outbound message is constructed with correct LXMF fields."""
        client = client_with_mocks

        dest_identity = MagicMock()
        dest_identity.hash = b"\x0a" * 16

        result = client._send_message(dest_identity, "Hello world")

        assert result is True, "Send should return True on success"

        # Verify LXMessage was constructed
        sys.modules["LXMF"].LXMessage.assert_called_once()
        call_kwargs = sys.modules["LXMF"].LXMessage.call_args.kwargs
        assert call_kwargs["title"] == "p:Envelope"
        assert call_kwargs["destination"] == dest_identity
        assert call_kwargs["source"] == client.client_identity
        assert call_kwargs["desired_method"] == 1  # OPPORTUNISTIC

        # Verify content is a serialized protobuf envelope
        reply_content = call_kwargs.get("content")
        assert reply_content is not None, "Message must have content"
        assert len(reply_content) > 0, "Message envelope must not be empty"

        # Verify handle_outbound was called
        client.router.handle_outbound.assert_called_once()

    def test_send_message_empty_content_rejected(self, client_with_mocks):
        """Empty message content should be rejected."""
        client = client_with_mocks

        dest_identity = MagicMock()

        result = client._send_message(dest_identity, "")

        assert result is False, "Send should return False for empty content"
        client.router.handle_outbound.assert_not_called()

    def test_send_message_whitespace_only_rejected(self, client_with_mocks):
        """Whitespace-only content should be rejected."""
        client = client_with_mocks

        dest_identity = MagicMock()

        result = client._send_message(dest_identity, "   ")

        assert result is False, "Send should return False for whitespace-only content"
        client.router.handle_outbound.assert_not_called()

    def test_send_message_without_router_returns_false(self, client_with_mocks):
        """Send should fail gracefully when router is None."""
        client = client_with_mocks
        client.router = None

        dest_identity = MagicMock()

        result = client._send_message(dest_identity, "Test")

        assert result is False, "Send should return False without router"

    def test_send_message_without_identity_returns_false(self, client_with_mocks):
        """Send should fail gracefully when client_identity is None."""
        client = client_with_mocks
        client.client_identity = None

        dest_identity = MagicMock()

        result = client._send_message(dest_identity, "Test")

        assert result is False, "Send should return False without identity"

    def test_send_message_handles_lxmf_exception(self, client_with_mocks):
        """Send should return False when LXMF raises LXMFException."""
        client = client_with_mocks

        LXMFException = sys.modules["LXMF"].LXMFException
        client.router.handle_outbound.side_effect = LXMFException("Delivery failed")

        dest_identity = MagicMock()
        dest_identity.hash = b"\x0b" * 16

        result = client._send_message(dest_identity, "Test message")

        assert result is False, "Send should return False on LXMFException"

    def test_send_message_handles_os_error(self, client_with_mocks):
        """Send should return False on OSError during message dispatch."""
        client = client_with_mocks

        client.router.handle_outbound.side_effect = OSError("Connection broken")

        dest_identity = MagicMock()
        dest_identity.hash = b"\x0c" * 16

        result = client._send_message(dest_identity, "Test message")

        assert result is False, "Send should return False on OSError"


if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))
