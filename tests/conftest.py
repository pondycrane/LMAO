"""Shared pytest fixtures and mock helpers for LMAO tests.

Provides ``setup_common_mocks()`` and ``cleanup_common_mocks()``
so that test files for the server and human client don't need to
duplicate the ~75-line sys.modules mock configuration.

``setup_common_mocks(with_grpc=True)`` sets up all the external
dependencies needed to import ``lmao_server.server`` or
``human_client.client``.  Call ``cleanup_common_mocks()`` in a
fixture teardown to prevent cross-test pollution.

RNS and LXMF are exposed through ``lma_core.rns_di`` (a
Dependency-Injection wrapper) so that production code imports
from a single point that can be monkeypatched in tests.
"""

import sys
import types
from unittest.mock import MagicMock


def setup_common_mocks(with_grpc=True):
    """Populate sys.modules with mocks for external dependencies.

    Must be called **before** importing server or client modules.
    When ``with_grpc`` is True (default), gRPC and proto stubs are
    also mocked so that the server's gRPC service tests work.
    """
    # ── Mock RNS and LXMF ──────────────────────────────────────────
    sys.modules["RNS"] = MagicMock()
    sys.modules["LXMF"] = MagicMock()

    # Mock proto.lma_pb2 so the real lma_core package can be imported
    _proto_pb2 = MagicMock()
    _proto_pb2.LMAOEnvelope = MagicMock()
    _proto_pb2.TextMessage = MagicMock()
    _proto_pb2.SensorReport = MagicMock()
    _proto_pb2.SensorReading = MagicMock()
    _proto_pb2.CommandRequest = MagicMock()
    _proto_pb2.CommandAck = MagicMock()
    _proto_pb2.AudioMessage = MagicMock()
    _proto_pb2.ImageMessage = MagicMock()
    _proto_pb2.CallSignal = MagicMock()
    _proto_pb2.SendRequest = MagicMock()
    _proto_pb2.SendResponse = MagicMock()
    _proto_pb2.SubscribeRequest = MagicMock()
    _proto_pb2.SubscribeResponse = MagicMock()
    _proto_pb2.GetIdentityRequest = MagicMock()
    _proto_pb2.GetIdentityResponse = MagicMock()
    sys.modules["proto"] = MagicMock()
    sys.modules["proto.lma_pb2"] = _proto_pb2

    # Import the real lma_core package (needs proto.lma_pb2 mocked first)
    import lma_core as _real_lma_core

    # Re-bind proto types on lma_core so they reference the fresh mocks
    for _attr in (
        "LMAOEnvelope", "TextMessage", "SensorReport", "SensorReading",
        "CommandRequest", "CommandAck", "AudioMessage", "ImageMessage",
        "CallSignal",
    ):
        setattr(_real_lma_core, _attr, getattr(_proto_pb2, _attr))

    sys.modules["lma_core"] = _real_lma_core

    # ── Patch rns_di with mock RNS / LXMF ──────────────────────────
    import lma_core.rns_di as _real_rns_di

    _real_rns_di.RNS = sys.modules["RNS"]
    _real_rns_di.LXMF = sys.modules["LXMF"]
    sys.modules["lma_core.rns_di"] = _real_rns_di

    # ── Register real rns_init (imports from patched rns_di) ───────
    import lma_core.rns_init as _real_rns_init

    sys.modules["lma_core.rns_init"] = _real_rns_init

    # ── Register real message_utils ────────────────────────────────
    import lma_core.message_utils as _real_msg_utils

    sys.modules["lma_core.message_utils"] = _real_msg_utils

    # ── Mock config ────────────────────────────────────────────────
    sys.modules["config"] = MagicMock()

    # ── Mock RNS types ─────────────────────────────────────────────
    sys.modules["RNS"].RNSException = type("RNSException", (Exception,), {})
    sys.modules["RNS"].hexrep = MagicMock(return_value="testhash1234")
    sys.modules["RNS"].Identity = MagicMock()
    sys.modules["RNS"].Identity.recall = MagicMock()
    sys.modules["RNS"].Reticulum = MagicMock()

    # ── Mock LXMF types ────────────────────────────────────────────
    sys.modules["LXMF"].LXMFException = type("LXMFException", (Exception,), {})
    sys.modules["LXMF"].LXMessage = MagicMock()
    sys.modules["LXMF"].LXMessage.OPPORTUNISTIC = 1
    sys.modules["LXMF"].LXMRouter = MagicMock()

    # ── Mock config module ─────────────────────────────────────────
    sys.modules["config"].get_configdir = MagicMock(return_value="/tmp/test_config")
    sys.modules["config"].get_config_dict = MagicMock(
        return_value={
            "interfaces": {"RNode LoRa": {"port": "/dev/ttyUSB0"}},
        }
    )

    if with_grpc:
        # ── Mock gRPC types ────────────────────────────────────────
        grpc_mock = MagicMock()
        grpc_mock.StatusCode.INVALID_ARGUMENT = MagicMock()
        grpc_mock.StatusCode.INTERNAL = MagicMock()
        grpc_mock.StatusCode.UNIMPLEMENTED = MagicMock()
        sys.modules["grpc"] = grpc_mock

        # Mock proto.lma_pb2_grpc
        proto_grpc_mock = MagicMock()
        sys.modules["proto.lma_pb2_grpc"] = proto_grpc_mock

        # Create mock lma_core.grpc_types module
        _grpc_types = types.ModuleType("lma_core.grpc_types")
        _grpc_types.SendRequest = MagicMock()
        _grpc_types.SendResponse = MagicMock()
        _grpc_types.SubscribeRequest = MagicMock()
        _grpc_types.SubscribeResponse = MagicMock()
        _grpc_types.GetIdentityRequest = MagicMock()
        _grpc_types.GetIdentityResponse = MagicMock()
        _grpc_types.LMAOStub = MagicMock()
        _grpc_types.LMAOServicer = type("LMAOServicer", (), {})
        _grpc_types.LMAO = MagicMock()
        _grpc_types.add_LMAOServicer_to_server = MagicMock()
        sys.modules["lma_core.grpc_types"] = _grpc_types

        # Backward compat: set on lma_core for direct imports
        for _attr in (
            "SendRequest", "SendResponse", "SubscribeRequest",
            "SubscribeResponse", "GetIdentityRequest", "GetIdentityResponse",
            "LMAOStub", "LMAOServicer", "LMAO", "add_LMAOServicer_to_server",
        ):
            setattr(_real_lma_core, _attr, getattr(_grpc_types, _attr))


def cleanup_common_mocks():
    """Remove mocked modules from sys.modules to prevent test pollution."""
    for mod in (
        "RNS",
        "LXMF",
        "config",
        "proto",
        "proto.lma_pb2",
        "proto.lma_pb2_grpc",
        "grpc",
        "lma_core.rns_di",
        "lma_core.rns_init",
        "lma_core.message_utils",
        "lma_core.grpc_types",
        "server",
        "lmao_server",
        "lmao_server.server",
        "client",
        "human_client",
        "human_client.client",
    ):
        if mod in sys.modules:
            del sys.modules[mod]
