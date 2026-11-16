#!/usr/bin/env python3
"""Print a native device's build-time provisioning variables.

Both native firmware builds ``eval`` this output, so provisioning is one
helper instead of per-tree logic (docs/identity-contact-design.md Phase 3):

  <PREFIX>DEST_HASH_HEX      the server's lxmf/delivery destination hash
  <PREFIX>SERVER_PUBKEY_HEX  the server's public key (build the destination
                             without ever hearing an announce, #142)
  <PREFIX>NODE_IDENTITY_HEX  the named device's 64-byte private key, when a
                             per-device identity exists on the host
                             (empty = keep the device's NVS identity)

``--prefix`` selects the firmware's variable names: ``LMAO_`` for the Sprout
(``LMAO_DEST_HASH_HEX`` …) and ``RH_`` for the RoboDog home client
(``RH_DEST_HASH_HEX`` …).

The server identity is resolved from the local store, else synced from the
in-cluster lmao-server PVC (issue #93).  It is NEVER minted here: a fresh
identity would silently break every flashed client (#70).

``--provision`` is the opt-in "pin this device" step: it mints the named
identity if absent, publishes its public record as an APPROVED contact (the
same flow ``install_all --device-name`` runs for a Cardputer), and prints the
key.  Without it, an existing ``devs/<name>/identity`` is pinned and an absent
one leaves the device on its NVS identity — so a plain build never changes a
fielded node's allow-listed delivery hash.
"""

from __future__ import annotations

import argparse
import os
import sys

from lma_core import client_identity, server_identity
from lma_core.contact_client import contacts_api_base, register_contact
from lma_core.rns_di import RNS


def _resolve_server_identity():
    """Load the server identity (local, else cluster); None when unavailable."""
    if RNS is None:
        raise ImportError("Reticulum (RNS) is not installed — cannot provision.")
    identity_file = server_identity.identity_file_path()
    if not os.path.isfile(identity_file):
        server_identity.sync_identity_from_cluster()
    if not os.path.isfile(identity_file):
        return None
    return RNS.Identity.from_file(identity_file)


def _device_identity(name: str | None, provision: bool,
                     device_type: str = "sprout") -> str:
    """The named device's private key hex, or "" when it must keep NVS.

    With *provision* the identity is minted when absent and registered as an
    approved contact; otherwise an absent identity stays empty (no hash drift
    for an already-fielded node).
    """
    if not name:
        return ""
    try:
        path = client_identity.identity_file_path(
            client_identity.device_identity_dir(name)
        )
    except ValueError as exc:
        raise SystemExit(f"device_provision: {exc}") from None
    if not os.path.isfile(path) and not provision:
        return ""

    identity, _ = client_identity.ensure_device_identity(name)
    if provision:
        delivery_hash = client_identity.device_delivery_destination_hash(name)
        pubkey_hex = client_identity.device_public_key_hex(name)
        if register_contact(name, delivery_hash, device_type, pubkey_hex):
            print(
                f"device_provision: registered {name!r} -> {delivery_hash[:12]}… approved",
                file=sys.stderr,
            )
        else:
            print(
                f"device_provision: WARNING could not register {name!r} at "
                f"{contacts_api_base()} — approve it with POST "
                f"{contacts_api_base()}/contacts/{delivery_hash}/approve "
                "(the LMAO_ALLOWED_CLIENTS seed only covers a device the "
                "server has never seen).",
                file=sys.stderr,
            )
    return identity.get_private_key().hex()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prefix",
        default="LMAO_",
        help="Variable prefix for the printed KEY=value lines (default LMAO_; "
             "the RoboDog home client uses RH_)",
    )
    parser.add_argument(
        "--device-name",
        default=os.environ.get("LMAO_DEVICE_NAME", "sprout"),
        help="Per-device identity name to pin (default: $LMAO_DEVICE_NAME or "
             "'sprout'; empty = keep the device's NVS identity)",
    )
    parser.add_argument(
        "--device-type",
        default="sprout",
        help="Contact-book device type for --provision (default: sprout)",
    )
    parser.add_argument(
        "--provision",
        action="store_true",
        help="Mint the named identity if absent and register it as an approved "
             "contact (pins the device to a stable delivery hash).",
    )
    args = parser.parse_args(argv)

    dest_var = f"{args.prefix}DEST_HASH_HEX"
    pubkey_var = f"{args.prefix}SERVER_PUBKEY_HEX"
    identity_var = f"{args.prefix}NODE_IDENTITY_HEX"

    try:
        identity = _resolve_server_identity()
        node_identity_hex = _device_identity(
            args.device_name, args.provision, args.device_type
        )
    except ImportError as exc:
        # A build host without Reticulum must still produce an announce-only
        # image (nothing else can pin).
        print(f"device_provision: {exc} — building announce-only.", file=sys.stderr)
        print(f"{dest_var}=")
        print(f"{pubkey_var}=")
        print(f"{identity_var}=")
        return 0

    if identity is None:
        print(
            "device_provision: no server identity found locally or in the cluster — "
            f"building announce-only ({dest_var}/{pubkey_var} empty).",
            file=sys.stderr,
        )
        print(f"{dest_var}=")
        print(f"{pubkey_var}=")
        print(f"{identity_var}={node_identity_hex}")
        return 0

    print(f"{dest_var}={server_identity.delivery_destination_hash_hex(identity)}")
    print(f"{pubkey_var}={identity.get_public_key().hex()}")
    print(f"{identity_var}={node_identity_hex}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
