"""Unit tests for lma_core.server_identity — identity persistence + DEST_HASH.

All RNS interactions are mocked: no real identity files are created and
no crypto operations run.

Run with::

    bazel test //tests:test_server_identity --test_output=all
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from lma_core import server_identity


@pytest.fixture
def mock_rns():
    """Swap lma_core.rns_di.RNS (already imported by server_identity) for a mock."""
    rns = MagicMock()
    rns.Destination.OUT = 1
    rns.Destination.SINGLE = 1
    rns.hexrep = lambda b, delimit=True: b.hex() if isinstance(b, bytes) else str(b)
    with patch.object(server_identity, "RNS", rns):
        yield rns


class TestIdentityFilePath:
    def test_default_path(self):
        path = server_identity.identity_file_path()
        assert path == os.path.join(server_identity.DEFAULT_IDENTITY_DIR, "identity")

    def test_custom_dir(self):
        assert server_identity.identity_file_path("/tmp/x") == "/tmp/x/identity"


class TestEnsureServerIdentity:
    def test_raises_without_rns(self, tmp_path):
        with patch.object(server_identity, "RNS", None):
            with pytest.raises(ImportError, match="RNS"):
                server_identity.ensure_server_identity(str(tmp_path))

    def test_loads_existing_identity(self, tmp_path, mock_rns):
        identity_file = tmp_path / "identity"
        identity_file.write_bytes(b"\x01" * 64)

        sentinel = MagicMock(name="identity")
        mock_rns.Identity.from_file.return_value = sentinel

        identity, path = server_identity.ensure_server_identity(str(tmp_path))

        assert identity is sentinel
        assert path == str(identity_file)
        mock_rns.Identity.from_file.assert_called_once_with(str(identity_file))
        # No new identity created
        mock_rns.Identity.assert_not_called()

    def test_creates_identity_when_missing(self, tmp_path, mock_rns):
        sentinel = MagicMock(name="identity")
        mock_rns.Identity.return_value = sentinel

        identity, path = server_identity.ensure_server_identity(str(tmp_path / "sub"))

        assert identity is sentinel
        assert os.path.dirname(path) == str(tmp_path / "sub")
        sentinel.to_file.assert_called_once_with(path)

    def test_recreates_when_load_fails(self, tmp_path, mock_rns):
        identity_file = tmp_path / "identity"
        identity_file.write_bytes(b"corrupt")
        mock_rns.Identity.from_file.side_effect = ValueError("bad identity")
        sentinel = MagicMock(name="identity")
        mock_rns.Identity.return_value = sentinel

        identity, _ = server_identity.ensure_server_identity(str(tmp_path))

        assert identity is sentinel
        sentinel.to_file.assert_called_once()


class TestDeliveryDestinationHash:
    def test_builds_lxmf_delivery_destination(self, mock_rns):
        dest = MagicMock()
        dest.hash = bytes.fromhex("ab" * 16)
        mock_rns.Destination.return_value = dest
        identity = MagicMock()

        result = server_identity.delivery_destination_hash_hex(identity)

        mock_rns.Destination.assert_called_once_with(
            identity,
            mock_rns.Destination.OUT,
            mock_rns.Destination.SINGLE,
            "lxmf",
            "delivery",
        )
        assert result == "ab" * 16

    def test_raises_without_rns(self):
        with patch.object(server_identity, "RNS", None):
            with pytest.raises(ImportError, match="RNS"):
                server_identity.delivery_destination_hash_hex(MagicMock())


class TestEnsureDeliveryDestinationHash:
    def test_chains_identity_and_hash(self, tmp_path, mock_rns):
        sentinel = MagicMock(name="identity")
        mock_rns.Identity.return_value = sentinel
        dest = MagicMock()
        dest.hash = bytes.fromhex("cd" * 16)
        mock_rns.Destination.return_value = dest

        result = server_identity.ensure_delivery_destination_hash(str(tmp_path))

        assert result == "cd" * 16


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))
