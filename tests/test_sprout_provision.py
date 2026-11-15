"""Tests for tools/sprout_provision.py — the Sprout build-time provisioning.

No hardware, no Postgres: the server identity and contact registration are
mocked.  These pin the two contracts build.sh relies on — the printed
KEY=value lines it evals, and that a plain build never changes a fielded
node's pinned identity.
"""

from __future__ import annotations

import sys
from unittest import mock

import pytest

from tools import sprout_provision


class _FakeIdentity:
    def get_public_key(self):
        return b"\xaa" * 64

    def get_private_key(self):
        return b"\xbb" * 64


_NO_IDENTITY = object()


def _run(argv, *, identity=_NO_IDENTITY, node_hex=""):
    lines = {}
    server_identity = _FakeIdentity() if identity is _NO_IDENTITY else identity

    def _capture(line):
        if "=" in line:
            lines[line.split("=", 1)[0]] = line.split("=", 1)[1]

    with (
        mock.patch.object(sprout_provision, "_resolve_server_identity",
                          return_value=server_identity),
        mock.patch.object(sprout_provision, "_device_identity",
                          return_value=node_hex),
        mock.patch.object(sprout_provision.server_identity,
                          "delivery_destination_hash_hex",
                          return_value="dad35b80164b25f7b1474be86e443702"),
        mock.patch("sys.stdout") as out,
        mock.patch("sys.stderr"),
    ):
        out.write.side_effect = _capture
        assert sprout_provision.main(argv) == 0
    return lines


def test_prints_the_three_build_variables():
    lines = _run(["--device-name", "sprout"])
    assert lines["LMAO_DEST_HASH_HEX"] == "dad35b80164b25f7b1474be86e443702"
    assert lines["LMAO_SERVER_PUBKEY_HEX"] == ("aa" * 64)
    assert lines["LMAO_NODE_IDENTITY_HEX"] == ""


def test_no_server_identity_builds_announce_only():
    lines = _run([], identity=None)
    assert lines["LMAO_DEST_HASH_HEX"] == ""
    assert lines["LMAO_SERVER_PUBKEY_HEX"] == ""


def test_provision_mints_and_registers_an_approved_contact(tmp_path, monkeypatch):
    """--provision mints the named identity and publishes it as an approved
    contact, the same provisioning step install_all runs for a Cardputer."""
    monkeypatch.setattr(sprout_provision.client_identity, "DEVICE_IDENTITY_ROOT",
                        str(tmp_path / "devs"))
    with (
        mock.patch.object(sprout_provision.client_identity, "ensure_device_identity",
                          return_value=(_FakeIdentity(), str(tmp_path / "id"))),
        mock.patch.object(sprout_provision.client_identity,
                          "device_delivery_destination_hash",
                          return_value="81aae19de81066b491f437d0e9820618"),
        mock.patch.object(sprout_provision.client_identity, "device_public_key_hex",
                          return_value="cc" * 64),
        mock.patch.object(sprout_provision, "register_contact",
                          return_value=True) as reg,
        mock.patch("sys.stderr"),
    ):
        key = sprout_provision._device_identity("sprout", provision=True)

    assert key == "bb" * 64
    reg.assert_called_once_with(
        "sprout", "81aae19de81066b491f437d0e9820618", "sprout", "cc" * 64
    )


def test_a_plain_build_does_not_mint_or_register(tmp_path, monkeypatch):
    """Without --provision an absent per-device identity stays empty, so the
    node keeps its NVS identity and its allow-listed delivery hash."""
    monkeypatch.setattr(sprout_provision.client_identity, "DEVICE_IDENTITY_ROOT",
                        str(tmp_path / "devs"))
    with (
        mock.patch.object(sprout_provision.client_identity, "ensure_device_identity",
                          side_effect=AssertionError("must not mint")) as mint,
        mock.patch.object(sprout_provision, "register_contact") as reg,
    ):
        assert sprout_provision._device_identity("sprout", provision=False) == ""
    assert not mint.called
    assert not reg.called
    assert not (tmp_path / "devs" / "sprout").exists()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
