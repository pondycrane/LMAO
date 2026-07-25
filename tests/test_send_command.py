"""Tests for lmao_server.send_command CLI tool.

Mocks Reticulum/LXMF dependencies so the tool can be tested
without real radio hardware.
"""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_rns_lxmf():
    """Populate sys.modules with mocks for RNS and LXMF."""
    sys.modules["RNS"] = MagicMock()
    sys.modules["LXMF"] = MagicMock()

    # Mock proto and lma_core
    _proto_pb2 = MagicMock()
    _proto_pb2.LMAOEnvelope = MagicMock()
    _proto_pb2.CommandRequest = MagicMock()
    _proto_pb2.CommandAck = MagicMock()
    sys.modules["proto"] = MagicMock()
    sys.modules["proto.lma_messages_pb2"] = _proto_pb2

    import lma_core as _real_lma_core

    _real_lma_core.CommandRequest = MagicMock()
    _real_lma_core.LMAOEnvelope = MagicMock()

    from lmao_server.server import Server

    yield

    # Cleanup
    for mod in ["RNS", "LXMF", "proto", "proto.lma_messages_pb2"]:
        sys.modules.pop(mod, None)


class TestMain:
    """Tests for send_command.main()."""

    def test_successful_dispatch_exits_zero(self, mock_rns_lxmf):
        """A valid command exits with code 0."""
        with patch.object(
            sys, "argv", ["send_command", "--target", "aabbcc", "--action", "reboot"]
        ), patch(
            "lmao_server.server._init_rns_and_lxmf"
        ) as mock_init, patch(
            "lmao_server.server.Server"
        ) as mock_server_cls:
            mock_init.return_value = (MagicMock(), MagicMock())
            mock_server = MagicMock()
            mock_server.send_command.return_value = True
            mock_server_cls.return_value = mock_server

            with pytest.raises(SystemExit) as exc_info:
                from lmao_server import send_command
                send_command.main()

            assert exc_info.value.code == 0
            mock_server.send_command.assert_called_once_with(
                target_identity_hex="aabbcc", action="reboot",
                params={}, timeout_ms=60000,
            )

    def test_invalid_json_params_exits_one(self, mock_rns_lxmf):
        """Invalid --params JSON exits with code 1."""
        with patch.object(
            sys, "argv", [
                "send_command", "--target", "aabb",
                "--params", "not-json",
            ]
        ):
            with pytest.raises(SystemExit) as exc_info:
                from lmao_server import send_command
                send_command.main()

            assert exc_info.value.code == 1

    def test_non_dict_json_params_exits_one(self, mock_rns_lxmf):
        """--params that is not a JSON object exits with code 1."""
        with patch.object(
            sys, "argv", [
                "send_command", "--target", "aabb",
                "--params", '"just a string"',
            ]
        ):
            with pytest.raises(SystemExit) as exc_info:
                from lmao_server import send_command
                send_command.main()

            assert exc_info.value.code == 1

    def test_dispatch_failure_exits_one(self, mock_rns_lxmf):
        """When send_command returns False, exits with code 1."""
        with patch.object(
            sys, "argv", ["send_command", "--target", "aabb"]
        ), patch(
            "lmao_server.server._init_rns_and_lxmf"
        ) as mock_init, patch(
            "lmao_server.server.Server"
        ) as mock_server_cls:
            mock_init.return_value = (MagicMock(), MagicMock())
            mock_server = MagicMock()
            mock_server.send_command.return_value = False
            mock_server_cls.return_value = mock_server

            with pytest.raises(SystemExit) as exc_info:
                from lmao_server import send_command
                send_command.main()

            assert exc_info.value.code == 1

    def test_rns_init_failure_exits_one(self, mock_rns_lxmf):
        """When RNS/LXMF init fails, exits with code 1."""
        with patch.object(
            sys, "argv", ["send_command", "--target", "aabb"]
        ), patch(
            "lmao_server.server._init_rns_and_lxmf",
            side_effect=RuntimeError("RNode not found"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                from lmao_server import send_command
                send_command.main()

            assert exc_info.value.code == 1
