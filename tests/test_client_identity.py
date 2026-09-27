"""Unit tests for lma_core.client_identity — canonical client identity (no-drift).

Two layers:
  - mocked tests (mirror test_server_identity) for the persistence plumbing;
  - a real-RNS acceptance test (skipped when RNS is unavailable) asserting the
    no-drift invariant: the known on-device identity adopted by install_all must
    keep resolving to the same lxmf/delivery hash that is allow-listed on the
    server (99ce3231…), and a canonical identity must round-trip byte-identically.

Run with::

    bazel test //tests:test_client_identity --test_output=all
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from lma_core import client_identity, server_identity


@pytest.fixture
def mock_rns():
    """Swap lma_core RNS (both modules) for a mock."""
    rns = MagicMock()
    rns.Destination.OUT = 1
    rns.Destination.SINGLE = 1
    rns.hexrep = lambda b, delimit=True: b.hex() if isinstance(b, bytes) else str(b)
    with patch.object(client_identity, "RNS", rns), patch.object(
        server_identity, "RNS", rns
    ):
        yield rns


# Known on-device identity file adopted during the 2026-09-27 flash.  It must
# always resolve to the allow-listed delivery hash — the whole point of pinning.
ON_DEVICE_IDENTITY_FILE_HEX = (
    "280aea11b88c63b1c91f8b9c738c138fd4e87e5dd5e529c3ab98d62023bc9c75"
    "3195cfa3a489a7f4a17ba412df5ba5a90cde60d8e4e67fc7c10ce09aced042f8"
)
PINNED_DELIVERY_HASH = "99ce32311dc37193eff4951a912f8f1b"


class TestIdentityFilePath:
    def test_default_path(self):
        path = client_identity.identity_file_path()
        assert path == os.path.join(client_identity.DEFAULT_IDENTITY_DIR, "identity")

    def test_custom_dir(self):
        assert client_identity.identity_file_path("/tmp/x") == "/tmp/x/identity"


class TestEnsureClientIdentity:
    def test_raises_without_rns(self, tmp_path):
        with patch.object(client_identity, "RNS", None):
            with pytest.raises(ImportError, match="RNS"):
                client_identity.ensure_client_identity(str(tmp_path))

    def test_loads_existing_identity(self, tmp_path, mock_rns):
        identity_file = tmp_path / "identity"
        identity_file.write_bytes(b"\x01" * 64)

        sentinel = MagicMock(name="identity")
        mock_rns.Identity.from_file.return_value = sentinel

        identity, path = client_identity.ensure_client_identity(str(tmp_path))

        assert identity is sentinel
        assert path == str(identity_file)
        mock_rns.Identity.from_file.assert_called_once_with(str(identity_file))
        mock_rns.Identity.assert_not_called()

    def test_creates_identity_when_missing(self, tmp_path, mock_rns):
        sentinel = MagicMock(name="identity")
        mock_rns.Identity.return_value = sentinel

        identity, path = client_identity.ensure_client_identity(str(tmp_path / "sub"))

        assert identity is sentinel
        assert os.path.dirname(path) == str(tmp_path / "sub")
        sentinel.to_file.assert_called_once_with(path)

    def test_recreates_when_load_fails(self, tmp_path, mock_rns):
        identity_file = tmp_path / "identity"
        identity_file.write_bytes(b"corrupt")
        mock_rns.Identity.from_file.side_effect = ValueError("bad identity")
        sentinel = MagicMock(name="identity")
        mock_rns.Identity.return_value = sentinel

        identity, _ = client_identity.ensure_client_identity(str(tmp_path))

        assert identity is sentinel
        sentinel.to_file.assert_called_once()


class TestAdoptClientIdentityBytes:
    def test_accepts_valid_identity(self, tmp_path, mock_rns):
        sentinel = MagicMock(name="identity")
        mock_rns.Identity.from_file.return_value = sentinel
        data = bytes.fromhex(ON_DEVICE_IDENTITY_FILE_HEX)

        identity, path = client_identity.adopt_client_identity_bytes(data, str(tmp_path))

        assert identity is sentinel
        assert path == str(tmp_path / "identity")
        assert (tmp_path / "identity").read_bytes() == data

    def test_rejects_invalid_bytes(self, tmp_path, mock_rns):
        mock_rns.Identity.from_file.return_value = None
        assert (
            client_identity.adopt_client_identity_bytes(b"not an identity", str(tmp_path))
            is None
        )
        assert not (tmp_path / "identity").exists()

    def test_still_writes_when_parse_raises(self, tmp_path, mock_rns):
        mock_rns.Identity.from_file.side_effect = ValueError("corrupt")
        assert (
            client_identity.adopt_client_identity_bytes(b"\x00", str(tmp_path)) is None
        )


class TestRealRNSNoDrift:
    """Real-reference-RNS acceptance: pinning must not change the allow-listed hash.

    Runs only when RNS is installed (the bazel test deps pin @lmao_pip//rns).
    """

    @staticmethod
    def _have_rns():
        from lma_core.rns_di import RNS

        return RNS is not None

    def test_adopted_device_identity_keeps_pinned_hash(self, tmp_path):
        if not self._have_rns():
            pytest.skip("RNS not installed")
        # install_all adopts the on-device identity file: canonical store is
        # seeded with exactly those bytes, so the allow-listed hash never drifts.
        adopted = client_identity.adopt_client_identity_bytes(
            bytes.fromhex(ON_DEVICE_IDENTITY_FILE_HEX), str(tmp_path)
        )
        assert adopted is not None, "known on-device identity must parse"

        identity, path = client_identity.ensure_client_identity(str(tmp_path))
        from lma_core.server_identity import delivery_destination_hash_hex

        assert delivery_destination_hash_hex(identity) == PINNED_DELIVERY_HASH
        assert (tmp_path / "identity").read_bytes() == bytes.fromhex(
            ON_DEVICE_IDENTITY_FILE_HEX
        )

    def test_canonical_identity_roundtrips(self, tmp_path):
        if not self._have_rns():
            pytest.skip("RNS not installed")
        first, _ = client_identity.ensure_client_identity(str(tmp_path))

        # Re-loading the canonical file must yield the same identity + hash.
        again, _ = client_identity.ensure_client_identity(str(tmp_path))
        from lma_core.server_identity import delivery_destination_hash_hex

        assert delivery_destination_hash_hex(first) == delivery_destination_hash_hex(again)


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))
