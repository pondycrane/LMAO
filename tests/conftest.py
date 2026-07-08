"""Shared pytest fixtures and mock helpers for LMAO tests.

Provides ``setup_common_mocks()`` and ``cleanup_common_mocks()``
so that test files for the server and human client don't need to
duplicate the ~75-line sys.modules mock configuration.

``setup_common_mocks(with_grpc=True)`` sets up all the external
dependencies needed to import ``lmao_server.server`` or
``human_client.client``.  Call ``cleanup_common_mocks()`` in a
fixture teardown to prevent cross-test pollution.
"""

import sys
from unittest.mock import MagicMock


def setup_common_mocks(with_grpc=True):
    """Populate sys.modules with mocks for external dependencies.

    Must be called **before** importing server or client modules.
    When ``with_grpc`` is True (default), gRPC and proto stubs are
    also mocked so that the server's gRPC service tests work.

    The real ``lma_core.message_utils`` is registered so that the
    ``decode_lmao_message`` import resolves, but the ``LMAOEnvelope``
    inside the function body is picked up lazily from the mock below.
    """
    sys.modules["RNS"] = MagicMock()
    sys.modules["LXMF"] = MagicMock()
    sys.modules["config"] = MagicMock()

    # Import the real message_utils module BEFORE mocking lma_core
    # so that server.py's ``from lma_core.message_utils import ...`` resolves.
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

    if with_grpc:
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


def cleanup_common_mocks():
    """Remove mocked modules from sys.modules to prevent test pollution."""
    for mod in [
        "RNS", "LXMF", "config", "lma_core", "lma_core.message_utils",
        "grpc", "proto", "proto.lma_pb2_grpc",
        "server", "lmao_server", "lmao_server.server",
        "client", "human_client", "human_client.client",
    ]:
        if mod in sys.modules:
            del sys.modules[mod]
