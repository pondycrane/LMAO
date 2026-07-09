"""Tests for server startup and lifecycle (with mocked RNS/LXMF)."""
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock
import builtins
import pytest
import sys

from conftest import setup_common_mocks, cleanup_common_mocks


@pytest.fixture
def server_with_main_mocks():
    """Set up mocks for testing server.main() with simulated Reticulum/LXMF.

    Provides a fresh import of the server module with all external
    dependencies mocked. Call server.main() to exercise the startup path.
    The main loop is terminated via KeyboardInterrupt raised from time.sleep.
    """
    if "server" in sys.modules:
        del sys.modules["server"]

    setup_common_mocks(with_grpc=True)

    # Import server after mocks are set up
    from lmao_server import server

    yield server

    cleanup_common_mocks()


class TestMain:
    """Tests for server.main() startup and initialization."""

    def test_main_successful_startup(self, server_with_main_mocks):
        """main() should initialize Reticulum and LXMF router, then loop until KeyboardInterrupt."""
        server = server_with_main_mocks

        # Configure mocks for happy path
        mock_identity = MagicMock()
        type(mock_identity).hash = PropertyMock(return_value=b"\x01" * 16)
        sys.modules["RNS"].Identity.return_value = mock_identity

        # Trigger KeyboardInterrupt to exit the infinite loop
        # start() now catches KeyboardInterrupt and returns naturally (no sys.exit)
        with patch.object(server.time, "sleep", side_effect=KeyboardInterrupt):
            server.main()

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

        sys.modules["RNS"].Identity.side_effect = sys.modules["RNS"].RNSException(
            "OOM in crypto"
        )

        with (
            patch.object(server.os.path, "exists", return_value=True),
            patch.object(server.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            with pytest.raises(SystemExit) as exc:
                server.main()

        assert exc.value.code == 1, (
            "Should exit with code 1 on identity creation failure"
        )
        captured = capsys.readouterr()
        assert "FATAL" in captured.out + captured.err, (
            "Output should indicate FATAL error"
        )

    def test_router_creation_failure(self, server_with_main_mocks, capsys):
        """main() should exit(1) when LXMF.LXMRouter() fails."""
        server = server_with_main_mocks

        mock_identity = MagicMock()
        type(mock_identity).hash = PropertyMock(return_value=b"\x01" * 16)
        sys.modules["RNS"].Identity.return_value = mock_identity
        sys.modules["LXMF"].LXMRouter.side_effect = sys.modules["LXMF"].LXMFException(
            "Storage unwritable"
        )

        with (
            patch.object(server.os.path, "exists", return_value=True),
            patch.object(server.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            with pytest.raises(SystemExit) as exc:
                server.main()

        assert exc.value.code == 1, "Should exit with code 1 on router creation failure"
        captured = capsys.readouterr()
        assert "FATAL" in captured.out + captured.err, (
            "Output should indicate FATAL error"
        )

    @pytest.mark.parametrize(
        "rnode_exists,expected_substr",
        [
            (True, "RNode on /dev/ttyUSB0"),
            (False, "RNode not connected"),
        ],
    )
    def test_banner_reflects_rnode_status(
        self, server_with_main_mocks, rnode_exists, expected_substr, capsys, caplog
    ):
        """Banner should show RNode status or warning based on port existence."""
        server = server_with_main_mocks

        mock_identity = MagicMock()
        type(mock_identity).hash = PropertyMock(return_value=b"\x01" * 16)
        sys.modules["RNS"].Identity.return_value = mock_identity

        # start() now returns naturally on KeyboardInterrupt, no sys.exit
        with (
            patch.object(server.os.path, "exists", return_value=rnode_exists),
            patch.object(server.time, "sleep", side_effect=KeyboardInterrupt),
        ):
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

    @pytest.mark.parametrize(
        "exc_cls_name,exc_msg,expected_err",
        [
            ("PermissionError", "Permission denied", "FATAL"),
            ("OSError", "Disk full", "FATAL"),
            ("RNSException", "RNS init failed", "FATAL"),
            ("Exception", "Generic error", "FATAL"),
        ],
    )
    def test_ret_init_failure_handling(
        self, server_with_main_mocks, exc_cls_name, exc_msg, expected_err, capsys
    ):
        """main() should print fatal error and exit(1) when Reticulum init fails."""
        server = server_with_main_mocks

        # Resolve exception class from builtins or mock modules
        if exc_cls_name == "RNSException":
            exc_cls = sys.modules["RNS"].RNSException
        else:
            exc_cls = getattr(builtins, exc_cls_name, None)
        assert exc_cls is not None, f"Unknown exception class: {exc_cls_name}"

        with (
            patch.object(server.os.path, "exists", return_value=True),
            patch.object(server.RNS, "Reticulum", side_effect=exc_cls(exc_msg)),
        ):
            with pytest.raises(SystemExit) as exc:
                server.main()

        assert exc.value.code == 1, "Should exit with code 1 on initialization failure"
        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert expected_err in output, "Output should indicate FATAL error"


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


class TestServerStart:
    """Tests for Server.start() sync entry point."""

    @pytest.fixture
    def server_and_mod(self):
        """Create a Server instance with mocked dependencies."""
        if "server" in sys.modules:
            del sys.modules["server"]
        setup_common_mocks(with_grpc=True)
        from lmao_server import server as mod

        # Configure mocks for happy path
        mock_identity = MagicMock()
        type(mock_identity).hash = PropertyMock(return_value=b"\x01" * 16)
        sys.modules["RNS"].Identity.return_value = mock_identity

        server_instance = mod.Server(
            config_dict={
                "interfaces": {"RNode LoRa": {"port": "/dev/ttyUSB0"}},
            }
        )

        yield server_instance, mod
        cleanup_common_mocks()

    def test_start_initializes_and_loops(self, server_and_mod, capsys):
        """start() should initialize Reticulum/LXMF and loop until KeyboardInterrupt."""
        server_inst, mod = server_and_mod

        with patch.object(mod.time, "sleep", side_effect=KeyboardInterrupt):
            # start() now returns naturally instead of sys.exit(0)
            server_inst.start()

        captured = capsys.readouterr()
        assert "Reticulum initialized" in captured.out
        assert "Listening for LXMF messages" in captured.out
        assert "Shutting down" in captured.out

        # Verify Reticulum was initialized
        sys.modules["RNS"].Reticulum.assert_called_once()
        sys.modules["RNS"].Identity.assert_called_once()
        sys.modules["LXMF"].LXMRouter.assert_called_once()

    def test_start_shows_rnode_warning_when_port_missing(self, server_and_mod, capsys):
        """start() should print warning when RNode port does not exist."""
        server_inst, mod = server_and_mod

        with (
            patch.object(mod.os.path, "exists", return_value=False),
            patch.object(mod.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            server_inst.start()

        captured = capsys.readouterr()
        assert "RNode port" in captured.out
        assert "not found" in captured.out

    def test_start_banner_shows_identity(self, server_and_mod, capsys):
        """start() banner should include the server identity hex."""
        server_inst, mod = server_and_mod

        with patch.object(mod.time, "sleep", side_effect=KeyboardInterrupt):
            server_inst.start()

        captured = capsys.readouterr()
        assert "testhash1234" in captured.out

    def test_start_uses_custom_config(self, server_and_mod, capsys):
        """start() should use the config_dict passed to constructor."""
        server_inst, mod = server_and_mod

        with (
            patch.object(mod.os.path, "exists", return_value=True),
            patch.object(mod.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            server_inst.start()

        captured = capsys.readouterr()
        assert "/dev/ttyUSB0" in captured.out


if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))
