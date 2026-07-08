"""Tests for server gRPC service (with mocked RNS/LXMF)."""
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
def grpc_service_with_mocks():
    """Set up mocks and create an LMAOGrpcService instance for testing."""
    if "server" in sys.modules:
        del sys.modules["server"]

    setup_common_mocks(with_grpc=True)

    from lmao_server import server

    # Ensure GRPC_AVAILABLE is True
    assert server.GRPC_AVAILABLE, "GRPC_AVAILABLE must be True for gRPC tests"

    server_instance = server.Server()
    server_instance.router = MagicMock()
    server_instance.server_identity = MagicMock()
    server_instance.server_identity.hash = b'\x01' * 16

    grpc_svc = server.LMAOGrpcService(server_instance)

    yield grpc_svc, server_instance

    cleanup_common_mocks()


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
        mock_env.ParseFromString.side_effect = DecodeError("invalid protobuf")

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
        sys.modules["RNS"].Identity.from_hex.side_effect = ValueError("bad hash")

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




if __name__ == "__main__":
    import pytest
    import sys
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
