"""Tests for human client startup and lifecycle (with mocked RNS/LXMF)."""

from unittest.mock import MagicMock, patch, PropertyMock
import builtins
import pytest
import sys

from conftest import setup_common_mocks, cleanup_common_mocks


@pytest.fixture
def client_with_main_mocks():
    """Set up mocks for testing client.main() with simulated Reticulum/LXMF.

    Provides a fresh import of the client module with all external
    dependencies mocked. Call client.main() to exercise the startup path.
    The main loop is terminated via KeyboardInterrupt raised from input.
    """
    if "client" in sys.modules:
        del sys.modules["client"]

    setup_common_mocks(with_grpc=False)

    # Import client after mocks are set up
    from human_client import client

    yield client

    cleanup_common_mocks()


class TestMain:
    """Tests for client.main() startup and initialization."""

    def test_main_successful_startup(self, client_with_main_mocks):
        """main() should initialize Reticulum and LXMF router, then loop until KeyboardInterrupt."""
        client = client_with_main_mocks

        # Configure mocks for happy path
        mock_identity = MagicMock()
        type(mock_identity).hash = PropertyMock(return_value=b"\x01" * 16)
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

        sys.modules["RNS"].Identity.side_effect = sys.modules["RNS"].RNSException(
            "OOM in crypto"
        )

        with patch.object(client.os.path, "exists", return_value=True):
            with pytest.raises(SystemExit) as exc:
                client.main()

        assert exc.value.code == 1, (
            "Should exit with code 1 on identity creation failure"
        )
        captured = capsys.readouterr()
        assert "FATAL" in captured.out + captured.err, (
            "Output should indicate FATAL error"
        )

    def test_router_creation_failure(self, client_with_main_mocks, capsys):
        """main() should exit(1) when LXMF.LXMRouter() fails with LXMFException."""
        client = client_with_main_mocks

        mock_identity = MagicMock()
        type(mock_identity).hash = PropertyMock(return_value=b"\x01" * 16)
        sys.modules["RNS"].Identity.return_value = mock_identity
        sys.modules["LXMF"].LXMRouter.side_effect = sys.modules["LXMF"].LXMFException(
            "Storage unwritable"
        )

        with patch.object(client.os.path, "exists", return_value=True):
            with pytest.raises(SystemExit) as exc:
                client.main()

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
        self, client_with_main_mocks, rnode_exists, expected_substr, capsys, caplog
    ):
        """Banner should show RNode status or warning based on port existence."""
        client = client_with_main_mocks

        mock_identity = MagicMock()
        type(mock_identity).hash = PropertyMock(return_value=b"\x01" * 16)
        sys.modules["RNS"].Identity.return_value = mock_identity

        with (
            patch.object(client.os.path, "exists", return_value=rnode_exists),
            patch.object(client, "input", side_effect=KeyboardInterrupt),
        ):
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
        self, client_with_main_mocks, exc_cls_name, exc_msg, expected_err, capsys
    ):
        """main() should print fatal error and exit(1) when Reticulum init fails."""
        client = client_with_main_mocks

        # Resolve exception class from builtins or mock modules
        if exc_cls_name == "RNSException":
            exc_cls = sys.modules["RNS"].RNSException
        else:
            exc_cls = getattr(builtins, exc_cls_name, None)
        assert exc_cls is not None, f"Unknown exception class: {exc_cls_name}"

        with (
            patch.object(client.os.path, "exists", return_value=True),
            patch.object(client.RNS, "Reticulum", side_effect=exc_cls(exc_msg)),
        ):
            with pytest.raises(SystemExit) as exc:
                client.main()

        assert exc.value.code == 1, "Should exit with code 1 on initialization failure"
        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert expected_err in output, "Output should indicate FATAL error"


class TestRNodeMissing:
    """Tests for graceful handling of missing RNode."""

    def test_rnode_missing_starts_wifi_only(
        self, client_with_main_mocks, capsys, caplog
    ):
        """When RNode port does not exist, client should start in WiFi-only mode."""
        client = client_with_main_mocks

        mock_identity = MagicMock()
        type(mock_identity).hash = PropertyMock(return_value=b"\x01" * 16)
        sys.modules["RNS"].Identity.return_value = mock_identity

        # RNode does NOT exist
        with (
            patch.object(client.os.path, "exists", return_value=False),
            patch.object(client, "input", side_effect=KeyboardInterrupt),
        ):
            client.main()

        captured = capsys.readouterr()
        assert "RNode not connected" in captured.out or "not found" in captured.out, (
            "Should indicate RNode is not connected"
        )

    def test_announce_failure_not_fatal(self, client_with_main_mocks, capsys):
        """If LXMF announce fails with RNSException, client should still start."""
        client = client_with_main_mocks

        mock_identity = MagicMock()
        type(mock_identity).hash = PropertyMock(return_value=b"\x01" * 16)
        sys.modules["RNS"].Identity.return_value = mock_identity

        RNSException = sys.modules["RNS"].RNSException
        mock_router = sys.modules["LXMF"].LXMRouter.return_value
        mock_router.announce.side_effect = RNSException("Announce failed")

        with (
            patch.object(client.os.path, "exists", return_value=True),
            patch.object(client, "input", side_effect=KeyboardInterrupt),
        ):
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
        type(mock_identity).hash = PropertyMock(return_value=b"\x01" * 16)
        sys.modules["RNS"].Identity.return_value = mock_identity

        with (
            patch.object(client.os.path, "exists", return_value=True),
            patch.object(client, "input", side_effect=EOFError()),
        ):
            client.main()

        captured = capsys.readouterr()
        assert "Shutting down" in captured.out


if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))
