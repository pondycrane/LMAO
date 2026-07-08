"""Tests for human client message handler (with mocked RNS/LXMF)."""
from unittest.mock import MagicMock, patch, PropertyMock
import builtins
import pytest
import sys
from google.protobuf.message import DecodeError


def _setup_common_mocks():
    """Populate sys.modules with mocks for external dependencies.

    Must be called before importing the client module.
    Sets up mocked RNS, LXMF, config, and lma_core modules
    with default return values suitable for most tests.
    """
    sys.modules["RNS"] = MagicMock()
    sys.modules["LXMF"] = MagicMock()
    sys.modules["config"] = MagicMock()

    # Import the real message_utils module BEFORE mocking lma_core
    # so that client.py's ``from lma_core.message_utils import ...`` resolves.
    # The lazy import of LMAOEnvelope inside decode_lmao_message picks up
    # the mock configured below at call time.
    import lma_core.message_utils as _real_msg_utils

    sys.modules["lma_core"] = MagicMock()
    sys.modules["lma_core"].LMAOEnvelope = MagicMock()
    sys.modules["lma_core"].TextMessage = MagicMock()
    sys.modules["lma_core.message_utils"] = _real_msg_utils

    # Mock RNS types
    sys.modules["RNS"].RNSException = type("RNSException", (Exception,), {})
    sys.modules["RNS"].hexrep = MagicMock(return_value="testhash1234")
    sys.modules["RNS"].Identity = MagicMock()
    sys.modules["RNS"].Identity.recall = MagicMock()
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
    for mod in ["RNS", "LXMF", "config", "lma_core", "lma_core.message_utils",
                "client", "human_client", "human_client.client"]:
        if mod in sys.modules:
            del sys.modules[mod]


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

    _setup_common_mocks()

    # Configure mock envelope so protobuf decode raises DecodeError (triggering fallback)
    mock_envelope = MagicMock()
    mock_envelope.ParseFromString.side_effect = DecodeError("Test decode error")
    mock_envelope.SerializeToString.return_value = b"mock-serialized-envelope"
    sys.modules["lma_core"].LMAOEnvelope.return_value = mock_envelope

    from human_client import client
    client_instance = client.Client()
    client_instance.router = MagicMock()
    client_instance.client_identity = MagicMock()
    client_instance.client_identity.hash = b'\x01' * 16

    yield client_instance

    _cleanup_common_mocks()


@pytest.fixture
def client_with_main_mocks():
    """Set up mocks for testing client.main() with simulated Reticulum/LXMF.

    Provides a fresh import of the client module with all external
    dependencies mocked. Call client.main() to exercise the startup path.
    The main loop is terminated via KeyboardInterrupt raised from input.
    """
    if "client" in sys.modules:
        del sys.modules["client"]

    _setup_common_mocks()

    # Import client after mocks are set up
    from human_client import client

    yield client

    _cleanup_common_mocks()


class TestMain:
    """Tests for client.main() startup and initialization."""

    def test_main_successful_startup(self, client_with_main_mocks):
        """main() should initialize Reticulum and LXMF router, then loop until KeyboardInterrupt."""
        client = client_with_main_mocks

        # Configure mocks for happy path
        mock_identity = MagicMock()
        type(mock_identity).hash = PropertyMock(return_value=b'\x01' * 16)
        sys.modules["RNS"].Identity.return_value = mock_identity

        # Trigger KeyboardInterrupt to exit the infinite loop (now uses break, not sys.exit)
        with patch.object(client, "input", side_effect=KeyboardInterrupt):
            client.main()

        # Verify Reticulum was initialized
        sys.modules["RNS"].Reticulum.assert_called_once()

        # Verify identity was created
        sys.modules["RNS"].Identity.assert_called_once()

        # Verify LXMF router was created and callback registered
        sys.modules["LXMF"].LXMRouter.assert_called_once_with(
            identity=mock_identity, storagepath="/tmp/lmao_human_client_lxmf"
        )
        mock_router = sys.modules["LXMF"].LXMRouter.return_value
        mock_router.register_delivery_callback.assert_called_once()
        # Verify the callback is a bound method of Client
        callback = mock_router.register_delivery_callback.call_args[0][0]
        assert callable(callback), "Delivery callback should be callable"

        # Verify announce was called
        mock_router.announce.assert_called_once()

    def test_identity_creation_failure(self, client_with_main_mocks, capsys):
        """main() should exit(1) when RNS.Identity() fails with RNSException."""
        client = client_with_main_mocks

        sys.modules["RNS"].Identity.side_effect = sys.modules["RNS"].RNSException("OOM in crypto")

        with patch.object(client.os.path, "exists", return_value=True):
            with pytest.raises(SystemExit) as exc:
                client.main()

        assert exc.value.code == 1, "Should exit with code 1 on identity creation failure"
        captured = capsys.readouterr()
        assert "FATAL" in captured.out + captured.err, "Output should indicate FATAL error"

    def test_router_creation_failure(self, client_with_main_mocks, capsys):
        """main() should exit(1) when LXMF.LXMRouter() fails with LXMFException."""
        client = client_with_main_mocks

        mock_identity = MagicMock()
        type(mock_identity).hash = PropertyMock(return_value=b'\x01' * 16)
        sys.modules["RNS"].Identity.return_value = mock_identity
        sys.modules["LXMF"].LXMRouter.side_effect = sys.modules["LXMF"].LXMFException("Storage unwritable")

        with patch.object(client.os.path, "exists", return_value=True):
            with pytest.raises(SystemExit) as exc:
                client.main()

        assert exc.value.code == 1, "Should exit with code 1 on router creation failure"
        captured = capsys.readouterr()
        assert "FATAL" in captured.out + captured.err, "Output should indicate FATAL error"

    @pytest.mark.parametrize("rnode_exists,expected_substr", [
        (True, "RNode on /dev/ttyUSB0"),
        (False, "RNode not connected"),
    ])
    def test_banner_reflects_rnode_status(self, client_with_main_mocks, rnode_exists, expected_substr, capsys, caplog):
        """Banner should show RNode status or warning based on port existence."""
        client = client_with_main_mocks

        mock_identity = MagicMock()
        type(mock_identity).hash = PropertyMock(return_value=b'\x01' * 16)
        sys.modules["RNS"].Identity.return_value = mock_identity

        with patch.object(client.os.path, "exists", return_value=rnode_exists), \
             patch.object(client, "input", side_effect=KeyboardInterrupt):
            client.main()

        captured = capsys.readouterr()
        assert expected_substr in captured.out, (
            f"Expected '{expected_substr}' in banner output when rnode_exists={rnode_exists}"
        )

        # When RNode is missing, warning should be printed before banner
        if not rnode_exists:
            assert "not found" in captured.out, "Should print RNode-not-found warning"
            # Verify logger.warning was also emitted
            assert any(
                record.levelname == "WARNING" and "not found" in record.message
                for record in caplog.records
            ), "logger.warning should contain RNode port not found message"

    @pytest.mark.parametrize("exc_cls_name,exc_msg,expected_err", [
        ("PermissionError", "Permission denied", "FATAL"),
        ("OSError", "Disk full", "FATAL"),
        ("RNSException", "RNS init failed", "FATAL"),
        ("Exception", "Generic error", "FATAL"),
    ])
    def test_ret_init_failure_handling(self, client_with_main_mocks, exc_cls_name, exc_msg, expected_err, capsys):
        """main() should print fatal error and exit(1) when Reticulum init fails."""
        client = client_with_main_mocks

        # Resolve exception class from builtins or mock modules
        if exc_cls_name == "RNSException":
            exc_cls = sys.modules["RNS"].RNSException
        else:
            exc_cls = getattr(builtins, exc_cls_name, None)
        assert exc_cls is not None, f"Unknown exception class: {exc_cls_name}"

        with patch.object(client.os.path, "exists", return_value=True), \
             patch.object(client.RNS, "Reticulum", side_effect=exc_cls(exc_msg)):
            with pytest.raises(SystemExit) as exc:
                client.main()

        assert exc.value.code == 1, "Should exit with code 1 on initialization failure"
        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert expected_err in output, "Output should indicate FATAL error"


class TestHandleLXMFDelivery:
    """Tests for client message handling (receive path)."""

    def test_message_displayed_for_valid_message(self, client_with_mocks, capsys):
        """Handle valid message and verify it is displayed to terminal."""
        client = client_with_mocks

        msg = MagicMock()
        msg.get_source.return_value = MagicMock()
        msg.get_source.return_value.hash = b'\x02' * 16
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
        msg.get_source.return_value.hash = b'\x02' * 16
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
        msg.get_source.return_value.hash = b'\x03' * 16
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
        msg.get_source.return_value.hash = b'\x04' * 16
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
        msg.get_source.return_value.hash = b'\x05' * 16
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
        msg.get_source.return_value.hash = b'\x06' * 16
        msg.content = b"\xff\xfe\xfd\xfc\x00"
        msg.title_as_string.return_value = "p:Envelope"

        client.handle_lxmf_delivery(msg)
        # Should not crash
        captured = capsys.readouterr()
        assert "MSG from" in captured.out
        assert "non-text" in captured.out or "bytes" in captured.out

    def test_protobuf_decode_uses_content_from_text_field(self, client_with_mocks, capsys):
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
        msg.get_source.return_value.hash = b'\x07' * 16
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
        msg.get_source.return_value.hash = b'\x08' * 16
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
        msg.get_source.return_value.hash = b'\x09' * 16
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


class TestSendMessage:
    """Tests for client message sending."""

    def test_send_message_constructs_lxmf_correctly(self, client_with_mocks):
        """Verify outbound message is constructed with correct LXMF fields."""
        client = client_with_mocks

        dest_identity = MagicMock()
        dest_identity.hash = b'\x0a' * 16

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
        dest_identity.hash = b'\x0b' * 16

        result = client._send_message(dest_identity, "Test message")

        assert result is False, "Send should return False on LXMFException"

    def test_send_message_handles_os_error(self, client_with_mocks):
        """Send should return False on OSError during message dispatch."""
        client = client_with_mocks

        client.router.handle_outbound.side_effect = OSError("Connection broken")

        dest_identity = MagicMock()
        dest_identity.hash = b'\x0c' * 16

        result = client._send_message(dest_identity, "Test message")

        assert result is False, "Send should return False on OSError"


class TestInputParsing:
    """Tests for REPL input parsing commands."""

    @pytest.fixture
    def client_parsed(self, client_with_mocks):
        """Provide a client set up for input parsing tests."""
        return client_with_mocks

    def test_help_command(self, client_parsed, capsys):
        """'/help' should print available commands."""
        client = client_parsed

        result = client._parse_input("/help")

        assert result is True, "Help should keep the client running"
        captured = capsys.readouterr()
        assert "/send" in captured.out
        assert "/dest" in captured.out
        assert "/quit" in captured.out

    def test_quit_command(self, client_parsed):
        """'/quit' should return False to signal exit."""
        client = client_parsed

        result = client._parse_input("/quit")

        assert result is False, "Quit should signal exit"

    def test_exit_command(self, client_parsed):
        """'/exit' should also return False."""
        client = client_parsed

        result = client._parse_input("/exit")

        assert result is False, "Exit should signal exit"

    def test_empty_input_returns_true(self, client_parsed):
        """Empty input should keep the client running."""
        client = client_parsed

        result = client._parse_input("")

        assert result is True, "Empty input should not exit"

    def test_dest_command_valid_hash(self, client_parsed, capsys):
        """'/dest <valid_hash>' should set the default destination."""
        client = client_parsed

        valid_hash = "a" * 32
        mock_recalled = MagicMock()
        sys.modules["RNS"].Identity.recall.return_value = mock_recalled

        result = client._parse_input(f"/dest {valid_hash}")

        assert result is True
        assert client._default_dest_hash == valid_hash
        assert client._default_dest_identity == mock_recalled
        captured = capsys.readouterr()
        assert valid_hash in captured.out

    def test_dest_command_invalid_hash(self, client_parsed, capsys):
        """'/dest <invalid>' should print error."""
        client = client_parsed

        result = client._parse_input("/dest nothex")

        assert result is True
        assert "Invalid" in capsys.readouterr().out

    def test_dest_command_wrong_length(self, client_parsed, capsys):
        """'/dest <wrong_length>' should print error."""
        client = client_parsed

        result = client._parse_input("/dest abcd")

        assert result is True
        captured = capsys.readouterr()
        assert "Invalid" in captured.out or "32" in captured.out

    def test_dest_command_no_args(self, client_parsed, capsys):
        """/dest without argument should print error message."""
        client = client_parsed

        result = client._parse_input("/dest")

        assert result is True  # Should NOT shut down
        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert "Usage" in output or "destination" in output.lower(), (
            "Should display usage/error message"
        )

    def test_send_command_no_args(self, client_parsed, capsys):
        """'/send' without args should show usage."""
        client = client_parsed

        result = client._parse_input("/send")

        assert result is True
        captured = capsys.readouterr()
        assert "Usage" in captured.out

    def test_send_command_valid(self, client_parsed):
        """'/send <hash> <msg>' should send a message."""
        client = client_parsed

        valid_hash = "b" * 32
        mock_dest = MagicMock()
        mock_dest.hash = bytes.fromhex(valid_hash)
        sys.modules["RNS"].Identity.recall.return_value = mock_dest

        # Make _send_message succeed
        with patch.object(client, "_send_message", return_value=True) as mock_send:
            result = client._parse_input(f"/send {valid_hash} Hello")

            assert result is True
            mock_send.assert_called_once_with(mock_dest, "Hello")

    def test_send_command_invalid_hash(self, client_parsed, capsys):
        """'/send <bad_hash> ...' should print error."""
        client = client_parsed

        result = client._parse_input("/send badhash Hello")

        assert result is True
        assert "Invalid" in capsys.readouterr().out

    def test_send_command_empty_content(self, client_parsed, capsys):
        """'/send <hash> <empty>' should print error."""
        client = client_parsed

        valid_hash = "c" * 32
        result = client._parse_input(f"/send {valid_hash} ")

        assert result is True
        captured = capsys.readouterr()
        assert "empty" in captured.out.lower() or "Usage" in captured.out

    def test_plain_text_without_default_dest(self, client_parsed, capsys):
        """Plain text without a default dest should print guidance."""
        client = client_parsed
        client._default_dest_hash = None

        result = client._parse_input("Hello there")

        assert result is True
        captured = capsys.readouterr()
        assert "default" in captured.out.lower() or "/dest" in captured.out

    def test_plain_text_with_default_dest(self, client_parsed):
        """Plain text with default dest set should send."""
        client = client_parsed

        valid_hash = "d" * 32
        client._default_dest_hash = valid_hash
        mock_dest = MagicMock()
        mock_dest.hash = bytes.fromhex(valid_hash)
        client._default_dest_identity = mock_dest

        with patch.object(client, "_send_message", return_value=True) as mock_send:
            result = client._parse_input("Hello default")

            assert result is True
            mock_send.assert_called_once_with(mock_dest, "Hello default")

    def test_validate_hash_valid(self, client_parsed):
        """_validate_hash should accept valid 32-char hex string."""
        client = client_parsed

        valid_hash = "a" * 32
        ok, err = client._validate_hash(valid_hash)
        assert ok is True
        assert err is None

    def test_validate_hash_empty(self, client_parsed):
        """_validate_hash should reject empty string."""
        client = client_parsed

        ok, err = client._validate_hash("")
        assert ok is False
        assert err is not None

    def test_validate_hash_non_hex(self, client_parsed):
        """_validate_hash should reject non-hex characters."""
        client = client_parsed

        ok, err = client._validate_hash("g" * 32)
        assert ok is False
        assert err is not None

    def test_validate_hash_wrong_length(self, client_parsed):
        """_validate_hash should reject wrong-length strings."""
        client = client_parsed

        ok, err = client._validate_hash("abcd")
        assert ok is False
        assert err is not None
        assert "32" in err

    def test_dest_command_recall_failure(self, client_parsed, capsys):
        """'/dest <hash>' should warn when identity recall fails."""
        client = client_parsed

        valid_hash = "e" * 32
        RNSException = sys.modules["RNS"].RNSException
        sys.modules["RNS"].Identity.recall.side_effect = RNSException("Not found")

        result = client._parse_input(f"/dest {valid_hash}")

        assert result is True
        assert client._default_dest_hash == valid_hash
        assert client._default_dest_identity is None
        captured = capsys.readouterr()
        assert "Warning" in captured.out, "Should print warning on recall failure"
        assert "Could not resolve" in captured.out

    def test_plain_text_lazy_recall_success(self, client_parsed):
        """Plain text should lazy-recall identity when _default_dest_identity is None."""
        client = client_parsed

        valid_hash = "f" * 32
        client._default_dest_hash = valid_hash
        client._default_dest_identity = None

        mock_recalled = MagicMock()
        mock_recalled.hash = bytes.fromhex(valid_hash)
        sys.modules["RNS"].Identity.recall.return_value = mock_recalled

        with patch.object(client, "_send_message", return_value=True) as mock_send:
            result = client._parse_input("Lazy recall test")

            assert result is True
            assert client._default_dest_identity == mock_recalled, "Should cache recalled identity"
            mock_send.assert_called_once_with(mock_recalled, "Lazy recall test")

    def test_plain_text_lazy_recall_failure(self, client_parsed, capsys):
        """Plain text should print error when lazy recall fails."""
        client = client_parsed

        valid_hash = "10" * 16
        client._default_dest_hash = valid_hash
        client._default_dest_identity = None

        RNSException = sys.modules["RNS"].RNSException
        sys.modules["RNS"].Identity.recall.side_effect = RNSException("Not reachable")

        result = client._parse_input("This should fail")

        assert result is True
        captured = capsys.readouterr()
        assert "Error" in captured.out, "Should print error on lazy recall failure"

    def test_send_command_recall_returns_none(self, client_parsed, capsys):
        """'/send <hash> <msg>' should handle recall returning None."""
        client = client_parsed

        valid_hash = "11" * 16
        sys.modules["RNS"].Identity.recall.return_value = None

        result = client._parse_input(f"/send {valid_hash} Test")

        assert result is True
        captured = capsys.readouterr()
        assert "Error" in captured.out or "Could not resolve" in captured.out


class TestRNodeMissing:
    """Tests for graceful handling of missing RNode."""

    def test_rnode_missing_starts_wifi_only(self, client_with_main_mocks, capsys, caplog):
        """When RNode port does not exist, client should start in WiFi-only mode."""
        client = client_with_main_mocks

        mock_identity = MagicMock()
        type(mock_identity).hash = PropertyMock(return_value=b'\x01' * 16)
        sys.modules["RNS"].Identity.return_value = mock_identity

        # RNode does NOT exist
        with patch.object(client.os.path, "exists", return_value=False), \
             patch.object(client, "input", side_effect=KeyboardInterrupt):
            client.main()

        captured = capsys.readouterr()
        assert "RNode not connected" in captured.out or "not found" in captured.out, (
            "Should indicate RNode is not connected"
        )

    def test_announce_failure_not_fatal(self, client_with_main_mocks, capsys):
        """If LXMF announce fails with RNSException, client should still start."""
        client = client_with_main_mocks

        mock_identity = MagicMock()
        type(mock_identity).hash = PropertyMock(return_value=b'\x01' * 16)
        sys.modules["RNS"].Identity.return_value = mock_identity

        RNSException = sys.modules["RNS"].RNSException
        mock_router = sys.modules["LXMF"].LXMRouter.return_value
        mock_router.announce.side_effect = RNSException("Announce failed")

        with patch.object(client.os.path, "exists", return_value=True), \
             patch.object(client, "input", side_effect=KeyboardInterrupt):
            client.main()

        # Should still have printed the banner
        captured = capsys.readouterr()
        assert "LMAO Human Client" in captured.out


class TestReplInput:
    """Tests for REPL input handling and shutdown behavior."""

    def test_eof_error_handling(self, client_with_main_mocks, capsys):
        """EOFError should print Shutting down and exit gracefully (no SystemExit)."""
        client = client_with_main_mocks

        mock_identity = MagicMock()
        type(mock_identity).hash = PropertyMock(return_value=b'\x01' * 16)
        sys.modules["RNS"].Identity.return_value = mock_identity

        with patch.object(client.os.path, "exists", return_value=True), \
             patch.object(client, "input", side_effect=EOFError()):
            client.main()

        captured = capsys.readouterr()
        assert "Shutting down" in captured.out


if __name__ == "__main__":
    import pytest
    import sys
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
