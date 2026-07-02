"""Tests for server message handler (with mocked RNS/LXMF)."""
import asyncio
import logging
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock
import builtins
import pytest
import sys
from google.protobuf.message import DecodeError


def _setup_common_mocks():
    """Populate sys.modules with mocks for external dependencies.

    Must be called before importing the server module.
    Sets up mocked RNS, LXMF, config, lma_core, grpc, and proto modules
    with default return values suitable for most tests.
    """
    sys.modules["RNS"] = MagicMock()
    sys.modules["LXMF"] = MagicMock()
    sys.modules["config"] = MagicMock()
    sys.modules["lma_core"] = MagicMock()
    sys.modules["lma_core"].LMAOEnvelope = MagicMock()
    sys.modules["lma_core"].TextMessage = MagicMock()

    # Mock gRPC types
    grpc_mock = MagicMock()
    grpc_mock.StatusCode.INVALID_ARGUMENT = MagicMock()
    grpc_mock.StatusCode.INTERNAL = MagicMock()
    grpc_mock.StatusCode.UNIMPLEMENTED = MagicMock()
    sys.modules["grpc"] = grpc_mock

    # Mock proto module
    proto_grpc_mock = MagicMock()
    sys.modules["proto"] = MagicMock()
    sys.modules["proto.lma_pb2_grpc"] = proto_grpc_mock

    # Mock gRPC request/response types on lma_core
    sys.modules["lma_core"].SendRequest = MagicMock()
    sys.modules["lma_core"].SendResponse = MagicMock()
    sys.modules["lma_core"].SubscribeRequest = MagicMock()
    sys.modules["lma_core"].SubscribeResponse = MagicMock()
    sys.modules["lma_core"].TunnelRequest = MagicMock()
    sys.modules["lma_core"].TunnelResponse = MagicMock()
    sys.modules["lma_core"].GetIdentityRequest = MagicMock()
    sys.modules["lma_core"].GetIdentityResponse = MagicMock()
    # LMAOServicer must be a real class (used as base class for LMAOGrpcService)
    sys.modules["lma_core"].LMAOServicer = type("LMAOServicer", (), {})

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
    for mod in ["RNS", "LXMF", "config", "lma_core", "grpc", "proto",
                "proto.lma_pb2_grpc", "server", "lmao_server", "lmao_server.server"]:
        if mod in sys.modules:
            del sys.modules[mod]


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

    _setup_common_mocks()

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
    from lmao_server import server

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
        mock_router.register_delivery_callback.assert_called_once()
        # Verify the callback is a bound method of Server
        callback = mock_router.register_delivery_callback.call_args[0][0]
        assert callable(callback), "Delivery callback should be callable"

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
    def test_banner_reflects_rnode_status(self, server_with_main_mocks, rnode_exists, expected_substr, capsys, caplog):
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

    def test_protobuf_decode_uses_content_from_text_field(self, server_with_mocks):
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

        from lmao_server import server as server_mod
        with patch.object(server_mod.logger, 'info', wraps=server_mod.logger.info) as mock_log:
            server.handle_lxmf_delivery(msg)

        # Verify the protobuf content was logged
        found = any(
            call.args and "Content (protobuf)" in str(call.args[0])
            for call in mock_log.call_args_list
        )
        assert found, "Should log protobuf-decoded content"

    def test_protobuf_decode_non_text_uses_fallback(self, server_with_mocks):
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

        from lmao_server import server as server_mod
        with patch.object(server_mod.logger, 'warning', wraps=server_mod.logger.warning) as mock_warn, \
             patch.object(server_mod.logger, 'info', wraps=server_mod.logger.info) as mock_info:
            server.handle_lxmf_delivery(msg)

        # Verify fallback warning was logged
        warn_found = any(
            call.args and "non-text payload" in str(call.args[0])
            for call in mock_warn.call_args_list
        )
        assert warn_found, "Should log warning about non-text payload"

        # Verify raw text fallback was used
        info_found = any(
            call.args and "Content (raw text)" in str(call.args[0])
            for call in mock_info.call_args_list
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

        from lmao_server import server as server_mod
        with patch.object(server, '_fanout_to_grpc_subscribers', wraps=server._fanout_to_grpc_subscribers) as spy:
            server.handle_lxmf_delivery(msg)
            spy.assert_called_once_with(msg)


@pytest.fixture
def grpc_service_with_mocks():
    """Set up mocks and create an LMAOGrpcService instance for testing."""
    if "server" in sys.modules:
        del sys.modules["server"]

    _setup_common_mocks()

    from lmao_server import server

    # Ensure GRPC_AVAILABLE is True
    assert server.GRPC_AVAILABLE, "GRPC_AVAILABLE must be True for gRPC tests"

    server_instance = server.Server()
    server_instance.router = MagicMock()
    server_instance.server_identity = MagicMock()
    server_instance.server_identity.hash = b'\x01' * 16

    grpc_svc = server.LMAOGrpcService(server_instance)

    yield grpc_svc, server_instance

    _cleanup_common_mocks()


class TestLMAOGrpcService:
    """Tests for LMAOGrpcService RPC methods."""

    @pytest.mark.asyncio
    async def test_send_rpc_valid_envelope(self, grpc_service_with_mocks):
        """Send with valid destination should dispatch LXMF message."""
        grpc_svc, server_inst = grpc_service_with_mocks

        mock_context = AsyncMock()
        request = MagicMock()
        request.envelope = b"valid-envelope"
        request.destination_hash = "a1b2c3d4"

        # Mock Identity.from_hex
        mock_dest = MagicMock()
        sys.modules["RNS"].Identity.from_hex.return_value = mock_dest

        SendResponse = sys.modules["lma_core"].SendResponse
        SendResponse.side_effect = lambda **kw: MagicMock(**kw)

        from lmao_server import server as server_mod
        response = await grpc_svc.Send(request, mock_context)

        # Verify destination was resolved
        sys.modules["RNS"].Identity.from_hex.assert_called_once_with("a1b2c3d4")

        # Verify LXMF message was constructed with correct destination
        call_kwargs = sys.modules["LXMF"].LXMessage.call_args.kwargs
        assert call_kwargs["destination"] == mock_dest
        assert call_kwargs["title"] == "p:Envelope"

        # Verify router was called
        server_inst.router.handle_outbound.assert_called_once()

        # context.abort should NOT have been called
        mock_context.abort.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_rpc_invalid_envelope(self, grpc_service_with_mocks):
        """Send with bad envelope should abort with INVALID_ARGUMENT."""
        grpc_svc, server_inst = grpc_service_with_mocks

        mock_context = AsyncMock()
        request = MagicMock()
        request.envelope = b"\xff\xff\xff"

        mock_env = sys.modules["lma_core"].LMAOEnvelope.return_value
        mock_env.ParseFromString.side_effect = Exception("invalid protobuf")

        await grpc_svc.Send(request, mock_context)

        mock_context.abort.assert_called_once()
        args, _ = mock_context.abort.call_args
        assert args[0] == sys.modules["grpc"].StatusCode.INVALID_ARGUMENT

    @pytest.mark.asyncio
    async def test_send_rpc_invalid_destination(self, grpc_service_with_mocks):
        """Send with invalid destination hash should return error status."""
        grpc_svc, server_inst = grpc_service_with_mocks

        mock_context = AsyncMock()
        request = MagicMock()
        request.envelope = b"valid"
        request.destination_hash = "bad-hash"

        # from_hex raises
        sys.modules["RNS"].Identity.from_hex.side_effect = Exception("bad hash")

        SendResponse = sys.modules["lma_core"].SendResponse
        SendResponse.side_effect = lambda **kw: MagicMock(**kw)

        response = await grpc_svc.Send(request, mock_context)

        assert "error" in response.status

    @pytest.mark.asyncio
    async def test_send_rpc_empty_destination(self, grpc_service_with_mocks):
        """Send with empty destination_hash should return error."""
        grpc_svc, server_inst = grpc_service_with_mocks

        mock_context = AsyncMock()
        request = MagicMock()
        request.envelope = b"valid"
        request.destination_hash = ""

        SendResponse = sys.modules["lma_core"].SendResponse
        SendResponse.side_effect = lambda **kw: MagicMock(**kw)

        response = await grpc_svc.Send(request, mock_context)

        assert "error" in response.status

    @pytest.mark.asyncio
    async def test_send_rpc_dispatch_error(self, grpc_service_with_mocks):
        """Send should abort with INTERNAL when LXMF dispatch fails."""
        grpc_svc, server_inst = grpc_service_with_mocks

        mock_context = AsyncMock()
        request = MagicMock()
        request.envelope = b"valid"
        request.destination_hash = "a1b2c3d4"

        mock_dest = MagicMock()
        sys.modules["RNS"].Identity.from_hex.return_value = mock_dest

        # Router throws
        RNSException = sys.modules["RNS"].RNSException
        server_inst.router.handle_outbound.side_effect = RNSException("dispatch failed")

        await grpc_svc.Send(request, mock_context)

        mock_context.abort.assert_called_once()
        args, _ = mock_context.abort.call_args
        assert args[0] == sys.modules["grpc"].StatusCode.INTERNAL

    @pytest.mark.asyncio
    async def test_get_identity(self, grpc_service_with_mocks):
        """GetIdentity should return identity hex and node name."""
        grpc_svc, server_inst = grpc_service_with_mocks

        mock_context = AsyncMock()
        request = MagicMock()

        GetIdentityResponse = sys.modules["lma_core"].GetIdentityResponse
        GetIdentityResponse.side_effect = lambda **kw: MagicMock(**kw)

        response = await grpc_svc.GetIdentity(request, mock_context)

        assert response.identity_hex == "testhash1234"
        assert response.node_name == "lmao-server"

    @pytest.mark.asyncio
    async def test_subscribe_receives_messages(self, grpc_service_with_mocks):
        """Subscribe should yield events from the message queue."""
        grpc_svc, server_inst = grpc_service_with_mocks

        mock_context = AsyncMock()
        request = MagicMock()
        request.title_filter = ""

        SubscribeResponse = sys.modules["lma_core"].SubscribeResponse
        SubscribeResponse.side_effect = lambda **kw: MagicMock(**kw)

        # Start subscribe generator and advance to first await (blocks on queue.get)
        gen = grpc_svc.Subscribe(request, mock_context)
        recv_task = asyncio.ensure_future(gen.asend(None))
        # Let the generator start executing so it registers its queue
        await asyncio.sleep(0)

        # Get the queue that Subscribe registered and put a message
        q = server_inst._grpc_subscribers[0]
        msg = MagicMock()
        msg.content = b"hello"
        msg.get_source.return_value = MagicMock()
        msg.get_source.return_value.hash = b"\x01" * 16
        await q.put(msg)

        # Get first response
        resp = await asyncio.wait_for(recv_task, timeout=1.0)
        assert resp.envelope == b"hello"
        assert resp.source_hash == "testhash1234"

        # Cancel the generator to clean up
        await gen.aclose()

    @pytest.mark.asyncio
    async def test_subscribe_title_filter(self, grpc_service_with_mocks):
        """Subscribe with title_filter should only forward matching messages."""
        grpc_svc, server_inst = grpc_service_with_mocks

        mock_context = AsyncMock()
        request = MagicMock()
        request.title_filter = "p:Envelope"

        SubscribeResponse = sys.modules["lma_core"].SubscribeResponse
        SubscribeResponse.side_effect = lambda **kw: MagicMock(**kw)

        # Start subscribe generator and advance to first await (blocks on queue.get)
        gen = grpc_svc.Subscribe(request, mock_context)
        recv_task = asyncio.ensure_future(gen.asend(None))
        # Let the generator start executing so it registers its queue
        await asyncio.sleep(0)

        # Get the queue that Subscribe registered
        q = server_inst._grpc_subscribers[0]

        # Put a non-matching message first (will be filtered)
        msg_nomatch = MagicMock()
        msg_nomatch.content = b"nomatch"
        msg_nomatch.get_source.return_value = MagicMock()
        msg_nomatch.get_source.return_value.hash = b"\x02" * 16
        msg_nomatch.title_as_string.return_value = "other:Stuff"
        await q.put(msg_nomatch)

        # Put a matching message (will be yielded after nomatch is filtered)
        msg_match = MagicMock()
        msg_match.content = b"match"
        msg_match.get_source.return_value = MagicMock()
        msg_match.get_source.return_value.hash = b"\x01" * 16
        msg_match.title_as_string.return_value = "p:Envelope"
        await q.put(msg_match)

        # First yielded should be the match (nomatch is filtered)
        resp = await asyncio.wait_for(recv_task, timeout=1.0)
        assert resp.envelope == b"match"

        # Cancel the generator to clean up
        await gen.aclose()

    @pytest.mark.asyncio
    async def test_tunnel_returns_unimplemented(self, grpc_service_with_mocks):
        """Tunnel should abort with UNIMPLEMENTED."""
        grpc_svc, server_inst = grpc_service_with_mocks

        mock_context = AsyncMock()

        async def request_iter():
            req = MagicMock()
            req.packet = b"test"
            yield req

        gen = grpc_svc.Tunnel(request_iter(), mock_context)

        # Tunnel aborts context then falls through (no yield in first iteration)
        await gen.asend(None)

        mock_context.abort.assert_called_once()
        args, _ = mock_context.abort.call_args
        assert args[0] == sys.modules["grpc"].StatusCode.UNIMPLEMENTED


class TestAsyncMain:
    """Tests for async_main() entry point."""

    @pytest.mark.asyncio
    async def test_async_main_grpc_disabled(self, capsys, caplog):
        """async_main should run main loop even when gRPC is not available."""
        if "server" in sys.modules:
            del sys.modules["server"]

        _setup_common_mocks()

        # Force GRPC_AVAILABLE to False
        from lmao_server import server as server_mod
        original_grpc = server_mod.GRPC_AVAILABLE
        server_mod.GRPC_AVAILABLE = False

        # Make RNS init work
        mock_identity = MagicMock()
        type(mock_identity).hash = PropertyMock(return_value=b"\x01" * 16)
        sys.modules["RNS"].Identity.return_value = mock_identity

        # Make asyncio.sleep raise KeyboardInterrupt after one iteration
        with patch.object(server_mod.asyncio, "sleep",
                          side_effect=[None, KeyboardInterrupt]) as mock_sleep:
            await server_mod.async_main()

        captured = capsys.readouterr()
        assert "Running (async mode)" in captured.out
        assert "Reticulum initialized" in captured.out

        # Restore GRPC_AVAILABLE
        server_mod.GRPC_AVAILABLE = original_grpc
        _cleanup_common_mocks()


if __name__ == "__main__":
    import pytest
    import sys
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
