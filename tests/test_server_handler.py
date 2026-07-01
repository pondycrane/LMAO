"""Tests for server message handler (with mocked RNS/LXMF)."""
from unittest.mock import MagicMock, patch
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lmao_server"))


@pytest.fixture
def server_with_mocks():
    """Set up mocks for RNS, LXMF, and module globals.

    Replaces the real RNS/LXMF modules on the server module with mocks
    so we can test handle_lxmf_delivery without real Reticulum hardware.
    """
    # Force reload of server module to get fresh state
    if "server" in sys.modules:
        del sys.modules["server"]

    # Pre-populate sys.modules with mocks for the imports server.py expects
    sys.modules["RNS"] = MagicMock()
    sys.modules["LXMF"] = MagicMock()
    sys.modules["config"] = MagicMock()

    # Configure mock RNS
    sys.modules["RNS"].RNSException = type("RNSException", (Exception,), {})
    sys.modules["RNS"].hexrep = MagicMock(return_value="testhash1234")
    sys.modules["RNS"].Identity = MagicMock()
    sys.modules["RNS"].Reticulum = MagicMock()

    # Configure mock LXMF
    sys.modules["LXMF"].LXMFException = type("LXMFException", (Exception,), {})
    sys.modules["LXMF"].LXMessage = MagicMock()
    sys.modules["LXMF"].LXMessage.OPPORTUNISTIC = 1
    sys.modules["LXMF"].LXMRouter = MagicMock()

    # Configure mock config
    sys.modules["config"].get_configdir = MagicMock(return_value="/tmp/test_config")
    sys.modules["config"].get_config_dict = MagicMock(return_value={
        "interfaces": {"RNode LoRa": {"port": "/dev/ttyUSB0"}},
    })

    # Mock the protobuf module to avoid dependency on protobuf library
    sys.modules["lmao_server.proto.lma_pb2"] = MagicMock()
    sys.modules["lmao_server.proto.lma_pb2"].LMAOEnvelope = MagicMock()
    sys.modules["lmao_server.proto.lma_pb2"].TextMessage = MagicMock()

    # Configure mock envelope so protobuf decode raises DecodeError (triggering fallback)
    mock_envelope = MagicMock()
    mock_envelope.ParseFromString.side_effect = type("DecodeError", (Exception,), {})("Test decode error")
    sys.modules["lmao_server.proto.lma_pb2"].LMAOEnvelope.return_value = mock_envelope

    import server
    server.router = MagicMock()
    server.server_identity = MagicMock()
    server.server_identity.hash = b'\x01' * 16

    yield server

    # Cleanup
    del sys.modules["RNS"]
    del sys.modules["LXMF"]
    del sys.modules["config"]
    del sys.modules["lmao_server.proto.lma_pb2"]
    if "server" in sys.modules:
        del sys.modules["server"]


class TestHandleLXMFDelivery:
    def test_reply_sent_for_valid_message(self, server_with_mocks):
        """Handle valid message and verify reply is sent."""
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
        call_kwargs = server.LXMF.LXMessage.call_args.kwargs
        assert call_kwargs["title"] == "p:Envelope"
        assert call_kwargs["destination"] == msg.get_source.return_value

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
