"""Tests for human client REPL parsing (with mocked RNS/LXMF)."""

import sys
from unittest.mock import MagicMock, patch

import pytest
from conftest import cleanup_common_mocks, setup_common_mocks
from google.protobuf.message import DecodeError


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

    setup_common_mocks(with_grpc=False)

    # Configure mock envelope so protobuf decode raises DecodeError (triggering fallback)
    mock_envelope = MagicMock()
    mock_envelope.ParseFromString.side_effect = DecodeError("Test decode error")
    mock_envelope.SerializeToString.return_value = b"mock-serialized-envelope"
    sys.modules["lma_core"].LMAOEnvelope.return_value = mock_envelope

    from human_client import client

    client_instance = client.Client()
    client_instance.router = MagicMock()
    client_instance.client_identity = MagicMock()
    client_instance.client_identity.hash = b"\x01" * 16

    yield client_instance

    cleanup_common_mocks()


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


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__] + sys.argv[1:]))
