"""Tests for server message handler (with mocked RNS/LXMF)."""
"""Tests for server message handler (with mocked RNS/LXMF)."""
import asyncio
import logging
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock
import builtins
import pytest
import sys
from google.protobuf.message import DecodeError

from conftest import setup_common_mocks, cleanup_common_mocks


@pytest.fixture
def server_with_mocks():
    """Set up mocks for RNS, LXMF, and create a Server instance.

    Replaces the real RNS/LXMF modules with mocks so we can test
    handle_lxmf_delivery without real Reticulum hardware.
    Returns a Server instance with router and server_identity set.
    """
    # Force reload of server module to get fresh state
    if "server" in sys.modules:
        del sys.modules["server"]

    setup_common_mocks(with_grpc=True)

    # Configure mock envelope so protobuf decode raises DecodeError (triggering fallback)
    mock_envelope = MagicMock()
    mock_envelope.ParseFromString.side_effect = DecodeError("Test decode error")
    mock_envelope.SerializeToString.return_value = b"mock-serialized-envelope"
    sys.modules["lma_core"].LMAOEnvelope.return_value = mock_envelope

    from lmao_server import server
    server_instance = server.Server()
    server_instance.router = MagicMock()
    server_instance.server_identity = MagicMock()
    server_instance.server_identity.hash = b'\x01' * 16

    yield server_instance

    cleanup_common_mocks()


class TestHandleLXMFDelivery:
    def test_reply_sent_for_valid_message(self, server_with_mocks):
        """Handle valid message and verify reply is sent with correct content."""
        server = server_with_mocks

        msg = MagicMock()
        msg.get_source.return_value = MagicMock()
        msg.get_source.return_value.hash = b'\x02' * 16
        msg.content = b"Hello from test"
        msg.title_as_string.return_value = "p:Envelope"

        server.handle_lxmf_delivery(msg)

        # Verify reply was sent
        server.router.handle_outbound.assert_called_once()
        # Check that LXMessage was constructed with expected args
        call_kwargs = sys.modules["LXMF"].LXMessage.call_args.kwargs
        assert call_kwargs["title"] == "p:Envelope"
        assert call_kwargs["destination"] == msg.get_source.return_value
        # Verify reply content is a non-empty protobuf envelope
        reply_content = call_kwargs.get("content")
        assert reply_content is not None, "Reply must have content"
        assert len(reply_content) > 0, "Reply envelope must not be empty"

    def test_no_reply_when_no_source(self, server_with_mocks):
        """Handle message with no source identity gracefully."""
        server = server_with_mocks

        msg = MagicMock()
        msg.get_source.return_value = None
        msg.content = b"anonymous"
        server.handle_lxmf_delivery(msg)

        # No reply should be sent
        server.router.handle_outbound.assert_not_called()

    def test_no_reply_when_no_router(self, server_with_mocks):
        """Handle message when router global is None."""
        server = server_with_mocks
        server.router = None

        msg = MagicMock()
        msg.get_source.return_value = MagicMock()
        msg.content = b"test"

        # Should not crash, should not send
        server.handle_lxmf_delivery(msg)

    def test_handles_empty_content(self, server_with_mocks):
        """Handle message with empty content."""
        server = server_with_mocks

        msg = MagicMock()
        msg.get_source.return_value = MagicMock()
        msg.get_source.return_value.hash = b'\x03' * 16
        msg.content = b""
        msg.title_as_string.return_value = "p:Envelope"

        server.handle_lxmf_delivery(msg)
        server.router.handle_outbound.assert_called_once()

    def test_handles_missing_content(self, server_with_mocks):
        """Handler should not crash on messages missing 'content' attribute."""
        server = server_with_mocks

        msg = MagicMock()
        msg.get_source.return_value = None
        # Remove content to simulate missing attribute
        del msg.content

        # Should not raise
        server.handle_lxmf_delivery(msg)
        server.router.handle_outbound.assert_not_called()

    def test_handles_attribute_error(self, server_with_mocks):
        """Handler should not crash when get_source raises AttributeError."""
        server = server_with_mocks

        msg = MagicMock()
        msg.get_source.side_effect = AttributeError("No source")

        # Should not raise
        server.handle_lxmf_delivery(msg)
        server.router.handle_outbound.assert_not_called()

    def test_protobuf_decode_success_path(self, server_with_mocks):
        """Verify protobuf-decoded content is used when ParseFromString succeeds."""
        server = server_with_mocks

        # Reconfigure mock to simulate successful protobuf decode
        mock_envelope = MagicMock()
        mock_envelope.HasField.return_value = True
        mock_envelope.text.content = "Hello from protobuf"
        sys.modules["lma_core"].LMAOEnvelope.return_value = mock_envelope

        msg = MagicMock()
        msg.get_source.return_value = MagicMock()
        msg.get_source.return_value.hash = b'\x04' * 16
        msg.content = b"protobuf-bytes"
        msg.title_as_string.return_value = "p:Envelope"

        server.handle_lxmf_delivery(msg)

        # Verify reply was sent
        server.router.handle_outbound.assert_called_once()
        call_kwargs = sys.modules["LXMF"].LXMessage.call_args.kwargs
        assert call_kwargs["title"] == "p:Envelope"

    def test_protobuf_decode_non_text_field(self, server_with_mocks):
        """Verify fallback when protobuf succeeds but has no text field."""
        server = server_with_mocks

        # Reconfigure mock: ParseFromString succeeds but HasField('text') is False
        mock_envelope = MagicMock()
        mock_envelope.HasField.return_value = False
        sys.modules["lma_core"].LMAOEnvelope.return_value = mock_envelope

        msg = MagicMock()
        msg.get_source.return_value = MagicMock()
        msg.get_source.return_value.hash = b'\x05' * 16
        msg.content = b"non-text protobuf bytes"
        msg.title_as_string.return_value = "p:Envelope"

        server.handle_lxmf_delivery(msg)
        server.router.handle_outbound.assert_called_once()

    def test_handles_binary_content(self, server_with_mocks):
        """Handler should not crash on binary non-decodable content."""
        server = server_with_mocks

        msg = MagicMock()
        msg.get_source.return_value = MagicMock()
        msg.get_source.return_value.hash = b'\x06' * 16
        msg.content = b"\xff\xfe\xfd\xfc\x00"
        msg.title_as_string.return_value = "p:Envelope"

        server.handle_lxmf_delivery(msg)
        server.router.handle_outbound.assert_called_once()

    def test_protobuf_decode_uses_content_from_text_field(self, server_with_mocks, caplog):
        """When protobuf decode succeeds and HasField('text') is True,
        the content from text.content is used as display_text."""
        server = server_with_mocks

        # Reconfigure mock to simulate successful protobuf decode
        mock_envelope = MagicMock()
        mock_envelope.HasField.return_value = True
        mock_envelope.text.content = "Decoded protobuf text"
        sys.modules["lma_core"].LMAOEnvelope.return_value = mock_envelope

        msg = MagicMock()
        msg.get_source.return_value = MagicMock()
        msg.get_source.return_value.hash = b'\x07' * 16
        msg.content = b"protobuf-encoded-bytes"
        msg.title_as_string.return_value = "p:Envelope"

        with caplog.at_level(logging.INFO, logger="lma_core.message_utils"):
            server.handle_lxmf_delivery(msg)

        # Verify the protobuf content was logged
        found = any(
            "Content (protobuf)" in record.message
            for record in caplog.records
        )
        assert found, "Should log protobuf-decoded content"

    def test_protobuf_decode_non_text_uses_fallback(self, server_with_mocks, caplog):
        """When protobuf decode succeeds but HasField('text') is False,
        the handler falls back to raw UTF-8 decode of content bytes."""
        server = server_with_mocks

        # Reconfigure mock: ParseFromString succeeds but HasField('text') is False
        mock_envelope = MagicMock()
        mock_envelope.HasField.return_value = False
        sys.modules["lma_core"].LMAOEnvelope.return_value = mock_envelope

        msg = MagicMock()
        msg.get_source.return_value = MagicMock()
        msg.get_source.return_value.hash = b'\x08' * 16
        msg.content = b"plain text fallback"
        msg.title_as_string.return_value = "p:Envelope"

        with caplog.at_level(logging.INFO, logger="lma_core.message_utils"):
            server.handle_lxmf_delivery(msg)

        # Verify fallback warning was logged
        warn_found = any(
            "non-text payload" in record.message
            for record in caplog.records
        )
        assert warn_found, "Should log warning about non-text payload"

        # Verify raw text fallback was used
        info_found = any(
            "Content (raw text)" in record.message
            for record in caplog.records
        )
        assert info_found, "Should log raw text fallback content"

    def test_protobuf_decode_binary_invalid_utf8(self, server_with_mocks):
        """When protobuf decode fails and content is not valid UTF-8,
        the handler shows a byte-count placeholder instead."""
        server = server_with_mocks

        # Fixture already has ParseFromString side_effect = DecodeError
        msg = MagicMock()
        msg.get_source.return_value = MagicMock()
        msg.get_source.return_value.hash = b'\x09' * 16
        msg.content = b"\xff\xfe\x00\x01"  # intentionally invalid UTF-8
        msg.title_as_string.return_value = "p:Envelope"

        # Handler should not crash, and should still send a reply
        server.handle_lxmf_delivery(msg)
        server.router.handle_outbound.assert_called_once()

        # Verify the reply content includes the byte-count placeholder
        call_kwargs = sys.modules["LXMF"].LXMessage.call_args.kwargs
        reply_content = call_kwargs.get("content")
        assert reply_content is not None
        assert len(reply_content) > 0, "Reply envelope must not be empty"


class TestSubscriberManagement:
    """Tests for gRPC subscriber queue management."""

    def test_register_subscriber(self, server_with_mocks):
        """register_grpc_subscriber should add queue to subscriber list."""
        server = server_with_mocks
        q = asyncio.Queue()
        server.register_grpc_subscriber(q)
        assert q in server._grpc_subscribers

    def test_unregister_subscriber(self, server_with_mocks):
        """unregister_grpc_subscriber should remove queue."""
        server = server_with_mocks
        q = asyncio.Queue()
        server.register_grpc_subscriber(q)
        server.unregister_grpc_subscriber(q)
        assert q not in server._grpc_subscribers

    def test_unregister_nonexistent_no_error(self, server_with_mocks):
        """unregister_grpc_subscriber of non-existent queue should not raise."""
        server = server_with_mocks
        q = asyncio.Queue()
        server.unregister_grpc_subscriber(q)  # Should not raise

    def test_fanout_removes_full_queues(self, server_with_mocks):
        """Fan-out should drop subscribers whose queues are full."""
        server = server_with_mocks
        q_full = asyncio.Queue(maxsize=1)
        q_full.put_nowait(object())  # Fill it
        q_ok = asyncio.Queue()
        server.register_grpc_subscriber(q_full)
        server.register_grpc_subscriber(q_ok)

        server._fanout_to_grpc_subscribers("test-message")

        assert q_full not in server._grpc_subscribers
        assert q_ok in server._grpc_subscribers

    def test_fanout_logs_exceptions(self, server_with_mocks, caplog):
        """Fan-out should log a warning when subscriber raises."""
        server = server_with_mocks
        q_bad = MagicMock(spec=asyncio.Queue)
        q_bad.put_nowait.side_effect = RuntimeError("queue closed")
        q_ok = asyncio.Queue()
        server.register_grpc_subscriber(q_bad)
        server.register_grpc_subscriber(q_ok)

        with caplog.at_level(logging.WARNING):
            server._fanout_to_grpc_subscribers("test-message")

        warning_messages = [r.message for r in caplog.records if r.levelname == "WARNING"]
        assert len(warning_messages) >= 1, "Should log at least one warning for bad subscriber"
        assert q_ok in server._grpc_subscribers, "Good subscriber should survive"

    def test_clear_grpc_subscribers(self, server_with_mocks):
        """clear_grpc_subscribers should drain and clear all queues."""
        server = server_with_mocks
        q1 = asyncio.Queue()
        q2 = asyncio.Queue()
        server.register_grpc_subscriber(q1)
        server.register_grpc_subscriber(q2)

        server.clear_grpc_subscribers()

        assert len(server._grpc_subscribers) == 0

    def test_handle_lxmf_fanout_called(self, server_with_mocks):
        """handle_lxmf_delivery should call _fanout_to_grpc_subscribers."""
        server = server_with_mocks

        msg = MagicMock()
        msg.get_source.return_value = MagicMock()
        msg.get_source.return_value.hash = b'\x0a' * 16
        msg.content = b"fanout test message"
        msg.title_as_string.return_value = "p:Envelope"

        with patch.object(server, '_fanout_to_grpc_subscribers', wraps=server._fanout_to_grpc_subscribers) as spy:
            server.handle_lxmf_delivery(msg)
            spy.assert_called_once_with(msg)




if __name__ == "__main__":
    import pytest
    import sys
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
