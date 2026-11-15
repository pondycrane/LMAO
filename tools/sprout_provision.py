#!/usr/bin/env python3
"""Print the Sprout native firmware's build-time provisioning variables.

``smart_irrigation/native-client/firmware/build.sh`` evals this output so a
Sprout build is self-provisioning — no hardcoded server literals in ``main.cpp``
(see docs/identity-contact-design.md Phase 3):

  LMAO_DEST_HASH_HEX      the server's lxmf/delivery destination hash
  LMAO_SERVER_PUBKEY_HEX  the server's public key (build the destination
                          without ever hearing an announce, #142)
  LMAO_NODE_IDENTITY_HEX  the named device's 64-byte private key, when a
                          per-device identity exists on the host
                          (empty = keep the device's NVS identity)

The server identity is resolved from the local store, else synced from the
in-cluster lmao-server PVC (issue #93).  It is NEVER minted here: a fresh
identity would silently break every flashed client (#70).

``--provision`` is the opt-in "pin this Sprout" step: it mints the named
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


def _device_identity(name: str | None, provision: bool) -> str:
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
        raise SystemExit(f"sprout_provision: {exc}") from None
    if not os.path.isfile(path) and not provision:
        return ""

    identity, _ = client_identity.ensure_device_identity(name)
    if provision:
        delivery_hash = client_identity.device_delivery_destination_hash(name)
        pubkey_hex = client_identity.device_public_key_hex(name)
        if register_contact(name, delivery_hash, "sprout", pubkey_hex):
            print(
                f"sprout_provision: registered {name!r} -> {delivery_hash[:12]}… approved",
                file=sys.stderr,
            )
        else:
            print(
                f"sprout_provision: WARNING could not register {name!r} at "
                f"{contacts_api_base()} — approve {delivery_hash} manually "
                f"(POST /contacts/{delivery_hash}/approve) or add it to "
                "LMAO_ALLOWED_CLIENTS.",
                file=sys.stderr,
            )
    return identity.get_private_key().hex()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--device-name",
        default=os.environ.get("LMAO_DEVICE_NAME", "sprout"),
        help="Per-device identity name to pin (default: $LMAO_DEVICE_NAME or "
             "'sprout'; empty = keep the device's NVS identity)",
    )
    parser.add_argument(
        "--provision",
        action="store_true",
        help="Mint the named identity if absent and register it as an approved "
             "contact (pins the Sprout to a stable delivery hash).",
    )
    args = parser.parse_args(argv)

    try:
        identity = _resolve_server_identity()
        node_identity_hex = _device_identity(args.device_name, args.provision)
    except ImportError as exc:
        # A build host without Reticulum must still produce an announce-only
        # image (the old committed literals are gone, so nothing else can pin).
        print(f"sprout_provision: {exc} — building announce-only.", file=sys.stderr)
        print("LMAO_DEST_HASH_HEX=")
        print("LMAO_SERVER_PUBKEY_HEX=")
        print("LMAO_NODE_IDENTITY_HEX=")
        return 0
    if identity is None:
        print(
            "sprout_provision: no server identity found locally or in the cluster — "
            "building announce-only (LMAO_DEST_HASH_HEX/LMAO_SERVER_PUBKEY_HEX empty).",
            file=sys.stderr,
        )
        print("LMAO_DEST_HASH_HEX=")
        print("LMAO_SERVER_PUBKEY_HEX=")
        print(f"LMAO_NODE_IDENTITY_HEX={node_identity_hex}")
        return 0

    print(
        "LMAO_DEST_HASH_HEX="
        f"{server_identity.delivery_destination_hash_hex(identity)}"
    )
    print(f"LMAO_SERVER_PUBKEY_HEX={identity.get_public_key().hex()}")
    print(f"LMAO_NODE_IDENTITY_HEX={node_identity_hex}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
