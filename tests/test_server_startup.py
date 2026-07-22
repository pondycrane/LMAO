"""Tests for server startup and lifecycle (with mocked RNS/LXMF)."""

import sys
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from conftest import cleanup_common_mocks, setup_common_mocks


class TestAsyncMain:
    """Tests for async_main() entry point."""

    @pytest.mark.asyncio
    async def test_async_main_grpc_disabled(self, capsys, caplog):
        """async_main should run main loop even when gRPC is not available."""
        for _mod in ("server", "lmao_server", "lmao_server.server"):
            if _mod in sys.modules:
                del sys.modules[_mod]

        setup_common_mocks(with_grpc=True)

        # Force GRPC_AVAILABLE to False and disable NATS
        from lmao_server import server as server_mod

        original_grpc = server_mod.GRPC_AVAILABLE
        original_nats = server_mod.NATS_AVAILABLE
        server_mod.GRPC_AVAILABLE = False
        server_mod.NATS_AVAILABLE = False

        # Make RNS init work
        mock_identity = MagicMock()
        type(mock_identity).hash = PropertyMock(return_value=b"\x01" * 16)
        sys.modules["RNS"].Identity.return_value = mock_identity

        # Make asyncio.sleep raise KeyboardInterrupt after one iteration
        with patch.object(server_mod.asyncio, "sleep", side_effect=[None, KeyboardInterrupt]):
            await server_mod.async_main()

        captured = capsys.readouterr()
        assert "Running (async mode)" in captured.out
        assert "Reticulum initialized" in captured.out

        # Restore
        server_mod.GRPC_AVAILABLE = original_grpc
        server_mod.NATS_AVAILABLE = original_nats
        cleanup_common_mocks()

    @pytest.mark.asyncio
    async def test_async_main_grpc_enabled(self, capsys, caplog):
        """async_main should start gRPC server when GRPC_AVAILABLE is True."""
        for _mod in ("server", "lmao_server", "lmao_server.server"):
            if _mod in sys.modules:
                del sys.modules[_mod]

        setup_common_mocks(with_grpc=True)

        from lmao_server import server as server_mod

        original_nats = server_mod.NATS_AVAILABLE
        server_mod.NATS_AVAILABLE = False
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

        # Mock add_LMAOServicer_to_server on lma_core.grpc_types (local import inside async_main)
        mock_add = MagicMock()
        sys.modules["lma_core.grpc_types"].add_LMAOServicer_to_server = mock_add

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

        server_mod.NATS_AVAILABLE = original_nats
        cleanup_common_mocks()


class TestAnnounceOnStartup:
    """Tests for router.announce() called during async_main."""

    @pytest.mark.asyncio
    async def test_announce_called_on_startup(self, capsys, caplog):
        """async_main should call router.announce() after registering callback."""
        for _mod in ("server", "lmao_server", "lmao_server.server"):
            if _mod in sys.modules:
                del sys.modules[_mod]

        setup_common_mocks(with_grpc=True)

        from lmao_server import server as server_mod

        # Disable both gRPC and NATS so only our loop runs
        original_grpc = server_mod.GRPC_AVAILABLE
        original_nats = server_mod.NATS_AVAILABLE
        server_mod.GRPC_AVAILABLE = False
        server_mod.NATS_AVAILABLE = False

        mock_identity = MagicMock()
        type(mock_identity).hash = PropertyMock(return_value=b"\x01" * 16)
        sys.modules["RNS"].Identity.return_value = mock_identity

        # Track announce calls.  The server announces each registered
        # delivery destination (see server._announce_delivery_destinations),
        # so the mock router must expose a delivery_destinations dict.
        mock_router = sys.modules["LXMF"].LXMRouter.return_value
        mock_router.delivery_destinations = {b"\x02" * 16: MagicMock()}

        # Make asyncio.sleep raise KeyboardInterrupt after two sleeps
        # (one normal sleep, one periodic check)
        with patch.object(
            server_mod.asyncio, "sleep", side_effect=[None, KeyboardInterrupt]
        ):
            await server_mod.async_main()

        captured = capsys.readouterr()
        assert "Running (async mode)" in captured.out

        # router.announce() should have been called at least once
        assert mock_router.announce.call_count >= 1, (
            f"Expected router.announce() to be called, got {mock_router.announce.call_count}"
        )

        # Restore
        server_mod.GRPC_AVAILABLE = original_grpc
        server_mod.NATS_AVAILABLE = original_nats
        cleanup_common_mocks()

    @pytest.mark.asyncio
    async def test_announce_failure_does_not_block_startup(self, capsys, caplog):
        """If router.announce() fails, server should still start."""
        for _mod in ("server", "lmao_server", "lmao_server.server"):
            if _mod in sys.modules:
                del sys.modules[_mod]

        setup_common_mocks(with_grpc=True)

        from lmao_server import server as server_mod

        original_grpc = server_mod.GRPC_AVAILABLE
        original_nats = server_mod.NATS_AVAILABLE
        server_mod.GRPC_AVAILABLE = False
        server_mod.NATS_AVAILABLE = False

        mock_identity = MagicMock()
        type(mock_identity).hash = PropertyMock(return_value=b"\x01" * 16)
        sys.modules["RNS"].Identity.return_value = mock_identity

        # Make announce raise an exception
        mock_router = sys.modules["LXMF"].LXMRouter.return_value
        mock_router.delivery_destinations = {b"\x02" * 16: MagicMock()}
        mock_router.announce.side_effect = OSError("RNode not connected")

        with patch.object(
            server_mod.asyncio, "sleep", side_effect=[None, KeyboardInterrupt]
        ):
            await server_mod.async_main()

        captured = capsys.readouterr()
        assert "Running (async mode)" in captured.out

        # Announce should still have been attempted
        assert mock_router.announce.called, (
            "router.announce() should have been called even though it raised"
        )

        # Server should have continued (banner printed)
        assert "LMAO Server" in captured.out

        server_mod.GRPC_AVAILABLE = original_grpc
        server_mod.NATS_AVAILABLE = original_nats
        cleanup_common_mocks()


class TestInitRnsAndLxmf:
    """Direct unit tests for _init_rns_and_lxmf()."""

    @pytest.fixture
    def server_mod(self):
        """Import server module with mocked dependencies."""
        for _mod in ("server", "lmao_server", "lmao_server.server"):
            if _mod in sys.modules:
                del sys.modules[_mod]
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
        # Default identity path is now ~/.local/share/lmao_server/lxmf
        _, kwargs = sys.modules["LXMF"].LXMRouter.call_args
        assert "storagepath" in kwargs
        assert kwargs["storagepath"].endswith(
            "/.local/share/lmao_server/lxmf"
        ), f"Expected persistent path, got {kwargs['storagepath']}"
        # Verify delivery identity is registered (required for receiving messages)
        router.register_delivery_identity.assert_called_once_with(
            identity, display_name="lmao-server"
        )

    def test_init_without_path_uses_default(self, server_mod):
        """_init_rns_and_lxmf should default to ~/.local/share/lmao_server/lxmf."""
        server_mod._init_rns_and_lxmf("/dev/ttyUSB0")
        _, kwargs = sys.modules["LXMF"].LXMRouter.call_args
        assert kwargs["storagepath"].endswith("/.local/share/lmao_server/lxmf"), (
            f"Expected persistent default, got {kwargs['storagepath']}"
        )

    def test_init_default_path_not_tmp(self, server_mod):
        """_init_rns_and_lxmf should NOT default to /tmp (survives reboots)."""
        server_mod._init_rns_and_lxmf("/dev/ttyUSB0")
        _, kwargs = sys.modules["LXMF"].LXMRouter.call_args
        assert not kwargs["storagepath"].startswith("/tmp"), (
            f"Identity path must be persistent, not {kwargs['storagepath']}"
        )

    def test_init_custom_storage_path(self, server_mod, tmp_path):
        """_init_rns_and_lxmf should pass custom identity_storage_path."""
        custom = str(tmp_path / "custom_lxmf")
        server_mod._init_rns_and_lxmf("/dev/ttyUSB0", identity_storage_path=custom)

        _, kwargs = sys.modules["LXMF"].LXMRouter.call_args
        assert kwargs.get("storagepath") == custom

    @pytest.mark.parametrize(
        "env_subpath",
        ["lxmf_a", "lxmf_b"],
    )
    def test_init_identity_path_from_env(self, server_mod, monkeypatch, tmp_path, env_subpath):
        """LMAO_SERVER_IDENTITY_PATH env var should override the default path."""
        env_value = str(tmp_path / env_subpath)
        monkeypatch.setenv("LMAO_SERVER_IDENTITY_PATH", env_value)
        server_mod._init_rns_and_lxmf("/dev/ttyUSB0")
        _, kwargs = sys.modules["LXMF"].LXMRouter.call_args
        assert kwargs["storagepath"] == env_value, (
            f"Env var should set path to {env_value}, got {kwargs['storagepath']}"
        )

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
        for _mod in ("server", "lmao_server", "lmao_server.server"):
            if _mod in sys.modules:
                del sys.modules[_mod]
        setup_common_mocks(with_grpc=True)
        from lmao_server import server as mod

        yield mod
        cleanup_common_mocks()

    def test_banner_shows_identity_and_status(self, server_mod, capsys):
        """Banner should include identity hex and RNode status."""
        with patch.object(server_mod.os.path, "exists", return_value=True):
            server_mod._print_startup_banner("testhash1234", "/dev/ttyUSB0", grpc_available=False)

        captured = capsys.readouterr()
        assert "testhash1234" in captured.out
        assert "RNode on /dev/ttyUSB0" in captured.out
        assert "LMAO Server — Running" in captured.out

    def test_banner_when_rnode_missing(self, server_mod, capsys):
        """Banner should show warning when RNode port does not exist."""
        with patch.object(server_mod.os.path, "exists", return_value=False):
            server_mod._print_startup_banner("testhash", "/dev/ttyUSB0", grpc_available=False)

        captured = capsys.readouterr()
        assert "RNode not connected" in captured.out

    def test_banner_with_grpc(self, server_mod, capsys):
        """Banner should include gRPC info when gRPC is available."""
        with patch.object(server_mod.os.path, "exists", return_value=True):
            server_mod._print_startup_banner("testhash", "/dev/ttyUSB0", grpc_available=True)

        captured = capsys.readouterr()
        assert "gRPC: 0.0.0.0:50051" in captured.out

    def test_banner_without_grpc(self, server_mod, capsys):
        """Banner should omit gRPC info when gRPC is not available."""
        with patch.object(server_mod.os.path, "exists", return_value=True):
            server_mod._print_startup_banner("testhash", "/dev/ttyUSB0", grpc_available=False)

        captured = capsys.readouterr()
        assert "gRPC:" not in captured.out

    def test_banner_includes_standard_sections(self, server_mod, capsys):
        """Banner should always contain key informational sections."""
        with patch.object(server_mod.os.path, "exists", return_value=True):
            server_mod._print_startup_banner("testhash", "/dev/ttyUSB0", grpc_available=False)

        captured = capsys.readouterr()
        assert "Listening for LXMF messages" in captured.out
        assert "WiFi: AutoInterface enabled" in captured.out
        assert "Title discriminator: p:Envelope" in captured.out

    def test_banner_shows_nats_connected(self, server_mod, capsys):
        """Banner should show NATS server URL when connected."""
        with patch.object(server_mod.os.path, "exists", return_value=True):
            server_mod._print_startup_banner(
                "testhash", "/dev/ttyUSB0", grpc_available=False, nats_connected=True
            )
        captured = capsys.readouterr()
        assert "NATS: nats://localhost:4222" in captured.out

    def test_banner_shows_nats_disconnected(self, server_mod, capsys):
        """Banner should show 'NATS: disconnected' when not connected."""
        with patch.object(server_mod.os.path, "exists", return_value=True):
            server_mod._print_startup_banner(
                "testhash", "/dev/ttyUSB0", grpc_available=False, nats_connected=False
            )
        captured = capsys.readouterr()
        assert "NATS: disconnected" in captured.out


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__] + sys.argv[1:]))
