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

        with patch.object(
            server_identity, "sync_identity_from_cluster", return_value=None
        ):
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

        with patch.object(
            server_identity, "sync_identity_from_cluster", return_value=None
        ):
            identity, _ = server_identity.ensure_server_identity(str(tmp_path))

        assert identity is sentinel
        sentinel.to_file.assert_called_once()

    def test_syncs_from_cluster_when_missing_locally(self, tmp_path, mock_rns):
        """No local identity + in-cluster server (issue #93): the identity is
        pulled from the pod instead of minting a mismatched one (#70)."""
        identity_file = tmp_path / "identity"

        def _fake_sync(identity_dir=None, **kwargs):
            identity_file.write_bytes(b"\x02" * 64)
            return str(identity_file)

        sentinel = MagicMock(name="identity")
        mock_rns.Identity.from_file.return_value = sentinel

        with patch.object(
            server_identity, "sync_identity_from_cluster", side_effect=_fake_sync
        ) as mock_sync:
            identity, path = server_identity.ensure_server_identity(str(tmp_path))

        mock_sync.assert_called_once()
        assert identity is sentinel
        assert path == str(identity_file)
        # No new identity created — the cluster one is authoritative
        mock_rns.Identity.assert_not_called()


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

        with patch.object(
            server_identity, "sync_identity_from_cluster", return_value=None
        ):
            result = server_identity.ensure_delivery_destination_hash(str(tmp_path))

        assert result == "cd" * 16


class TestSyncIdentityFromCluster:
    """Best-effort identity fetch from the in-cluster lmao-server pod (#93)."""

    @staticmethod
    def _proc(returncode=0, stdout=b"", text_stdout=""):
        proc = MagicMock()
        proc.returncode = returncode
        proc.stdout = stdout
        return proc

    def test_returns_none_without_kubectl(self, tmp_path):
        with patch("shutil.which", return_value=None):
            assert server_identity.sync_identity_from_cluster(str(tmp_path)) is None

    def test_returns_none_when_no_pod(self, tmp_path):
        with (
            patch("shutil.which", return_value="/usr/bin/kubectl"),
            patch("subprocess.run", return_value=self._proc(stdout=b"")),
        ):
            # jsonpath returns empty string when no pods match
            with patch("subprocess.run", return_value=self._proc()) as mock_run:
                mock_run.return_value.stdout = ""
                assert server_identity.sync_identity_from_cluster(str(tmp_path)) is None

    def test_returns_none_when_cat_fails(self, tmp_path):
        pod_proc = self._proc()
        pod_proc.stdout = "lmao-server-abc"
        cat_proc = self._proc(returncode=1, stdout=b"")
        with (
            patch("shutil.which", return_value="/usr/bin/kubectl"),
            patch("subprocess.run", side_effect=[pod_proc, cat_proc]),
        ):
            assert server_identity.sync_identity_from_cluster(str(tmp_path)) is None

    def test_writes_identity_bytes(self, tmp_path):
        pod_proc = self._proc()
        pod_proc.stdout = "lmao-server-abc"
        cat_proc = self._proc(stdout=b"\x7f" * 64)
        with (
            patch("shutil.which", return_value="/usr/bin/kubectl"),
            patch("subprocess.run", side_effect=[pod_proc, cat_proc]),
        ):
            result = server_identity.sync_identity_from_cluster(str(tmp_path))

        assert result == str(tmp_path / "identity")
        assert (tmp_path / "identity").read_bytes() == b"\x7f" * 64

    def test_never_raises_on_kubectl_timeout(self, tmp_path):
        import subprocess as sp

        with (
            patch("shutil.which", return_value="/usr/bin/kubectl"),
            patch("subprocess.run", side_effect=sp.TimeoutExpired("kubectl", 20)),
        ):
            assert server_identity.sync_identity_from_cluster(str(tmp_path)) is None


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))
