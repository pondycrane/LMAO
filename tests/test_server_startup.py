"""Tests for server startup and lifecycle (with mocked RNS/LXMF)."""
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock
import builtins
import pytest
import sys

from conftest import setup_common_mocks, cleanup_common_mocks


class TestAsyncMain:
    """Tests for async_main() entry point."""

    @pytest.mark.asyncio
    async def test_async_main_grpc_disabled(self, capsys, caplog):
        """async_main should run main loop even when gRPC is not available."""
        if "server" in sys.modules:
            del sys.modules["server"]

        setup_common_mocks(with_grpc=True)

        # Force GRPC_AVAILABLE to False
        from lmao_server import server as server_mod

        original_grpc = server_mod.GRPC_AVAILABLE
        server_mod.GRPC_AVAILABLE = False

        # Make RNS init work
        mock_identity = MagicMock()
        type(mock_identity).hash = PropertyMock(return_value=b"\x01" * 16)
        sys.modules["RNS"].Identity.return_value = mock_identity

        # Make asyncio.sleep raise KeyboardInterrupt after one iteration
        with patch.object(
            server_mod.asyncio, "sleep", side_effect=[None, KeyboardInterrupt]
        ):
            await server_mod.async_main()

        captured = capsys.readouterr()
        assert "Running (async mode)" in captured.out
        assert "Reticulum initialized" in captured.out

        # Restore GRPC_AVAILABLE
        server_mod.GRPC_AVAILABLE = original_grpc
        cleanup_common_mocks()

    @pytest.mark.asyncio
    async def test_async_main_grpc_enabled(self, capsys, caplog):
        """async_main should start gRPC server when GRPC_AVAILABLE is True."""
        if "server" in sys.modules:
            del sys.modules["server"]

        setup_common_mocks(with_grpc=True)

        from lmao_server import server as server_mod
        # GRPC_AVAILABLE is True by default with these mocks

        # Make RNS init work
        mock_identity = MagicMock()
        type(mock_identity).hash = PropertyMock(return_value=b"\x01" * 16)
        sys.modules["RNS"].Identity.return_value = mock_identity

        # Mock grpc.aio.server (start/stop are async, add_insecure_port is sync)
        mock_grpc_server = MagicMock()
        mock_grpc_server.start = AsyncMock()
        mock_grpc_server.wait_for_termination = AsyncMock()
        mock_grpc_server.stop = AsyncMock()
        sys.modules["grpc"].aio = MagicMock()
        sys.modules["grpc"].aio.server.return_value = mock_grpc_server

        # Mock add_LMAOServicer_to_server on lma_core (local import inside async_main)
        mock_add = MagicMock()
        sys.modules["lma_core"].add_LMAOServicer_to_server = mock_add

        # Make asyncio.sleep raise KeyboardInterrupt to exit
        with patch.object(server_mod.asyncio, "sleep", side_effect=KeyboardInterrupt):
            await server_mod.async_main()

        captured = capsys.readouterr()
        assert "Running (async mode)" in captured.out
        assert "gRPC server ready" in captured.out
        assert "gRPC: 0.0.0.0:50051" in captured.out

        # Verify gRPC server was started
        mock_grpc_server.start.assert_awaited_once()
        mock_grpc_server.add_insecure_port.assert_called_once_with("0.0.0.0:50051")
        mock_add.assert_called_once()

        # Verify cleanup on shutdown
        mock_grpc_server.stop.assert_awaited_once_with(5)

        cleanup_common_mocks()


class TestInitRnsAndLxmf:
    """Direct unit tests for _init_rns_and_lxmf()."""

    @pytest.fixture
    def server_mod(self):
        """Import server module with mocked dependencies."""
        if "server" in sys.modules:
            del sys.modules["server"]
        setup_common_mocks(with_grpc=True)
        from lmao_server import server as mod

        yield mod
        cleanup_common_mocks()

    def test_init_success_returns_identity_and_router(self, server_mod):
        """_init_rns_and_lxmf should return (identity, router) on success."""
        identity, router = server_mod._init_rns_and_lxmf("/dev/ttyUSB0")

        assert identity is not None
        assert router is sys.modules["LXMF"].LXMRouter.return_value
        sys.modules["RNS"].Reticulum.assert_called_once()
        sys.modules["RNS"].Identity.assert_called_once()
        sys.modules["LXMF"].LXMRouter.assert_called_once_with(
            identity=identity, storagepath="/tmp/lmao_server_lxmf"
        )

    def test_init_custom_storage_path(self, server_mod):
        """_init_rns_and_lxmf should pass custom identity_storage_path."""
        server_mod._init_rns_and_lxmf(
            "/dev/ttyUSB0", identity_storage_path="/custom/path"
        )

        _, kwargs = sys.modules["LXMF"].LXMRouter.call_args
        assert kwargs.get("storagepath") == "/custom/path"

    def test_init_exits_on_oserror(self, server_mod, capsys):
        """_init_rns_and_lxmf should sys.exit(1) on OSError from Reticulum init."""
        sys.modules["RNS"].Reticulum.side_effect = OSError("Disk full")

        with pytest.raises(SystemExit) as exc:
            server_mod._init_rns_and_lxmf("/dev/ttyUSB0")

        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "FATAL" in captured.out + captured.err

    def test_init_exits_on_rns_exception(self, server_mod, capsys):
        """_init_rns_and_lxmf should sys.exit(1) on RNSException from Reticulum init."""
        RNSException = sys.modules["RNS"].RNSException
        sys.modules["RNS"].Reticulum.side_effect = RNSException("Config error")

        with pytest.raises(SystemExit) as exc:
            server_mod._init_rns_and_lxmf("/dev/ttyUSB0")

        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "FATAL" in captured.out + captured.err

    def test_init_exits_on_identity_failure(self, server_mod, capsys):
        """_init_rns_and_lxmf should sys.exit(1) when identity creation fails."""
        RNSException = sys.modules["RNS"].RNSException
        sys.modules["RNS"].Identity.side_effect = RNSException("OOM")

        with pytest.raises(SystemExit) as exc:
            server_mod._init_rns_and_lxmf("/dev/ttyUSB0")

        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "FATAL" in captured.out + captured.err

    def test_init_exits_on_router_failure(self, server_mod, capsys):
        """_init_rns_and_lxmf should sys.exit(1) when LXMRouter creation fails."""
        mock_identity = MagicMock()
        sys.modules["RNS"].Identity.return_value = mock_identity
        LXMFException = sys.modules["LXMF"].LXMFException
        sys.modules["LXMF"].LXMRouter.side_effect = LXMFException("Storage unwritable")

        with pytest.raises(SystemExit) as exc:
            server_mod._init_rns_and_lxmf("/dev/ttyUSB0")

        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "FATAL" in captured.out + captured.err


class TestPrintStartupBanner:
    """Tests for _print_startup_banner() helper."""

    @pytest.fixture
    def server_mod(self):
        """Import server module with mocked dependencies."""
        if "server" in sys.modules:
            del sys.modules["server"]
        setup_common_mocks(with_grpc=True)
        from lmao_server import server as mod

        yield mod
        cleanup_common_mocks()

    def test_banner_shows_identity_and_status(self, server_mod, capsys):
        """Banner should include identity hex and RNode status."""
        with patch.object(server_mod.os.path, "exists", return_value=True):
            server_mod._print_startup_banner(
                "testhash1234", "/dev/ttyUSB0", grpc_available=False
            )

        captured = capsys.readouterr()
        assert "testhash1234" in captured.out
        assert "RNode on /dev/ttyUSB0" in captured.out
        assert "LMAO Server — Running" in captured.out

    def test_banner_when_rnode_missing(self, server_mod, capsys):
        """Banner should show warning when RNode port does not exist."""
        with patch.object(server_mod.os.path, "exists", return_value=False):
            server_mod._print_startup_banner(
                "testhash", "/dev/ttyUSB0", grpc_available=False
            )

        captured = capsys.readouterr()
        assert "RNode not connected" in captured.out

    def test_banner_with_grpc(self, server_mod, capsys):
        """Banner should include gRPC info when gRPC is available."""
        with patch.object(server_mod.os.path, "exists", return_value=True):
            server_mod._print_startup_banner(
                "testhash", "/dev/ttyUSB0", grpc_available=True
            )

        captured = capsys.readouterr()
        assert "gRPC: 0.0.0.0:50051" in captured.out

    def test_banner_without_grpc(self, server_mod, capsys):
        """Banner should omit gRPC info when gRPC is not available."""
        with patch.object(server_mod.os.path, "exists", return_value=True):
            server_mod._print_startup_banner(
                "testhash", "/dev/ttyUSB0", grpc_available=False
            )

        captured = capsys.readouterr()
        assert "gRPC:" not in captured.out

    def test_banner_includes_standard_sections(self, server_mod, capsys):
        """Banner should always contain key informational sections."""
        with patch.object(server_mod.os.path, "exists", return_value=True):
            server_mod._print_startup_banner(
                "testhash", "/dev/ttyUSB0", grpc_available=False
            )

        captured = capsys.readouterr()
        assert "Listening for LXMF messages" in captured.out
        assert "WiFi: AutoInterface enabled" in captured.out
        assert "Title discriminator: p:Envelope" in captured.out


if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))
