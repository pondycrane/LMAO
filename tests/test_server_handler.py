"""Tests for server message handler (with mocked RNS/LXMF)."""
from unittest.mock import MagicMock, patch, PropertyMock
import builtins
import pytest
import sys
from google.protobuf.message import DecodeError


def _setup_common_mocks():
    """Populate sys.modules with mocks for external dependencies.

    Must be called before importing the server module.
    Sets up mocked RNS, LXMF, config, and lma_core modules
    with default return values suitable for most tests.
    """
    sys.modules["RNS"] = MagicMock()
    sys.modules["LXMF"] = MagicMock()
    sys.modules["config"] = MagicMock()
    sys.modules["lma_core"] = MagicMock()
    sys.modules["lma_core"].LMAOEnvelope = MagicMock()
    sys.modules["lma_core"].TextMessage = MagicMock()

    # Mock RNS types
    sys.modules["RNS"].RNSException = type("RNSException", (Exception,), {})
    sys.modules["RNS"].hexrep = MagicMock(return_value="testhash1234")
    sys.modules["RNS"].Identity = MagicMock()
    sys.modules["RNS"].Reticulum = MagicMock()

    # Mock LXMF types
    sys.modules["LXMF"].LXMFException = type("LXMFException", (Exception,), {})
    sys.modules["LXMF"].LXMessage = MagicMock()
    sys.modules["LXMF"].LXMessage.OPPORTUNISTIC = 1
    sys.modules["LXMF"].LXMRouter = MagicMock()

    # Mock config module
    sys.modules["config"].get_configdir = MagicMock(return_value="/tmp/test_config")
    sys.modules["config"].get_config_dict = MagicMock(return_value={
        "interfaces": {"RNode LoRa": {"port": "/dev/ttyUSB0"}},
    })


def _cleanup_common_mocks():
    """Remove mocked modules from sys.modules to prevent test pollution."""
    for mod in ["RNS", "LXMF", "config", "lma_core", "server"]:
        if mod in sys.modules:
            del sys.modules[mod]


@pytest.fixture
def server_with_mocks():
    """Set up mocks for RNS, LXMF, and module globals.

    Replaces the real RNS/LXMF modules on the server module with mocks
    so we can test handle_lxmf_delivery without real Reticulum hardware.
    """
    # Force reload of server module to get fresh state
    if "server" in sys.modules:
        del sys.modules["server"]

    _setup_common_mocks()

    # Configure mock envelope so protobuf decode raises DecodeError (triggering fallback)
    mock_envelope = MagicMock()
    mock_envelope.ParseFromString.side_effect = DecodeError("Test decode error")
    mock_envelope.SerializeToString.return_value = b"mock-serialized-envelope"
    sys.modules["lma_core"].LMAOEnvelope.return_value = mock_envelope

    import server
    server.router = MagicMock()
    server.server_identity = MagicMock()
    server.server_identity.hash = b'\x01' * 16

    yield server

    _cleanup_common_mocks()


@pytest.fixture
def server_with_main_mocks():
    """Set up mocks for testing server.main() with simulated Reticulum/LXMF.

    Provides a fresh import of the server module with all external
    dependencies mocked. Call server.main() to exercise the startup path.
    The main loop is terminated via KeyboardInterrupt raised from time.sleep.
    """
    if "server" in sys.modules:
        del sys.modules["server"]

    _setup_common_mocks()

    # Import server after mocks are set up
    import server

    yield server

    _cleanup_common_mocks()


class TestMain:
    """Tests for server.main() startup and initialization."""

    def test_main_successful_startup(self, server_with_main_mocks):
        """main() should initialize Reticulum and LXMF router, then loop until KeyboardInterrupt."""
        server = server_with_main_mocks

        # Configure mocks for happy path
        mock_identity = MagicMock()
        type(mock_identity).hash = PropertyMock(return_value=b'\x01' * 16)
        sys.modules["RNS"].Identity.return_value = mock_identity

        # Trigger KeyboardInterrupt to exit the infinite loop
        with patch.object(server.time, "sleep", side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit) as exc:
                server.main()

        assert exc.value.code == 0, "Should exit with code 0 on KeyboardInterrupt"

        # Verify Reticulum was initialized
        sys.modules["RNS"].Reticulum.assert_called_once()

        # Verify identity was created
        sys.modules["RNS"].Identity.assert_called_once()

        # Verify LXMF router was created and callback registered
        sys.modules["LXMF"].LXMRouter.assert_called_once_with(
            identity=mock_identity, storagepath="/tmp/lmao_server_lxmf"
        )
        mock_router = sys.modules["LXMF"].LXMRouter.return_value
        mock_router.register_delivery_callback.assert_called_once_with(server.handle_lxmf_delivery)

    def test_identity_creation_failure(self, server_with_main_mocks, capsys):
        """main() should exit(1) when RNS.Identity() fails."""
        server = server_with_main_mocks

        sys.modules["RNS"].Identity.side_effect = Exception("OOM in crypto")

        with patch.object(server.os.path, "exists", return_value=True), \
             patch.object(server.time, "sleep", side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit) as exc:
                server.main()

        assert exc.value.code == 1, "Should exit with code 1 on identity creation failure"
        captured = capsys.readouterr()
        assert "FATAL" in captured.out + captured.err, "Output should indicate FATAL error"

    def test_router_creation_failure(self, server_with_main_mocks, capsys):
        """main() should exit(1) when LXMF.LXMRouter() fails."""
        server = server_with_main_mocks

        mock_identity = MagicMock()
        type(mock_identity).hash = PropertyMock(return_value=b'\x01' * 16)
        sys.modules["RNS"].Identity.return_value = mock_identity
        sys.modules["LXMF"].LXMRouter.side_effect = Exception("Storage unwritable")

        with patch.object(server.os.path, "exists", return_value=True), \
             patch.object(server.time, "sleep", side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit) as exc:
                server.main()

        assert exc.value.code == 1, "Should exit with code 1 on router creation failure"
        captured = capsys.readouterr()
        assert "FATAL" in captured.out + captured.err, "Output should indicate FATAL error"

    @pytest.mark.parametrize("rnode_exists,expected_substr", [
        (True, "RNode on /dev/ttyUSB0"),
        (False, "RNode not connected"),
    ])
    def test_banner_reflects_rnode_status(self, server_with_main_mocks, rnode_exists, expected_substr, capsys):
        """Banner should show RNode status or warning based on port existence."""
        server = server_with_main_mocks

        mock_identity = MagicMock()
        type(mock_identity).hash = PropertyMock(return_value=b'\x01' * 16)
        sys.modules["RNS"].Identity.return_value = mock_identity

        with patch.object(server.os.path, "exists", return_value=rnode_exists), \
             patch.object(server.time, "sleep", side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit):
                server.main()

        captured = capsys.readouterr()
        assert expected_substr in captured.out, (
            f"Expected '{expected_substr}' in banner output when rnode_exists={rnode_exists}"
        )

        # When RNode is missing, warning should be printed before banner
        if not rnode_exists:
            assert "not found" in captured.out, "Should print RNode-not-found warning"

    @pytest.mark.parametrize("exc_cls_name,exc_msg,expected_err", [
        ("PermissionError", "Permission denied", "FATAL"),
        ("OSError", "Disk full", "FATAL"),
        ("RNSException", "RNS init failed", "FATAL"),
        ("Exception", "Generic error", "FATAL"),
    ])
    def test_ret_init_failure_handling(self, server_with_main_mocks, exc_cls_name, exc_msg, expected_err, capsys):
        """main() should print fatal error and exit(1) when Reticulum init fails."""
        server = server_with_main_mocks

        # Resolve exception class from builtins or mock modules
        if exc_cls_name == "RNSException":
            exc_cls = sys.modules["RNS"].RNSException
        else:
            exc_cls = getattr(builtins, exc_cls_name, None)
        assert exc_cls is not None, f"Unknown exception class: {exc_cls_name}"

        with patch.object(server.os.path, "exists", return_value=True), \
             patch.object(server.RNS, "Reticulum", side_effect=exc_cls(exc_msg)):
            with pytest.raises(SystemExit) as exc:
                server.main()

        assert exc.value.code == 1, "Should exit with code 1 on initialization failure"
        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert expected_err in output, "Output should indicate FATAL error"


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
        call_kwargs = server.LXMF.LXMessage.call_args.kwargs
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
        call_kwargs = server.LXMF.LXMessage.call_args.kwargs
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


if __name__ == "__main__":
    import pytest
    import sys
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
