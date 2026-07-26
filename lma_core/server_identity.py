"""Server identity persistence + LXMF delivery destination hash helpers.

The Cardputer client bakes the server's ``lxmf.delivery`` *destination*
hash into its ``config.py`` as ``DEST_HASH``.  That hash is derived from
the server's Reticulum identity, so the identity must be **stable across
server restarts** — a fresh identity silently breaks every flashed client
(issue #70).

This module provides a single source of truth for:

  - :func:`ensure_server_identity` — load the persisted identity from
    ``~/.local/share/lmao_server/lxmf/identity``, creating and saving a
    new one when it does not exist yet.  Install tooling calls this
    *before* flashing clients and *before* starting the server container
    so both sides agree on the same identity.
  - :func:`delivery_destination_hash_hex` — compute the hex destination
    hash that clients must use as ``DEST_HASH``.

RNS is imported lazily via :mod:`lma_core.rns_di` so this module can be
imported without Reticulum installed; the public functions raise
``ImportError`` with a descriptive message when RNS is unavailable.
"""

from __future__ import annotations

import logging
import os

from lma_core.rns_di import RNS

_logger = logging.getLogger(__name__)

# Default identity directory — must match lmao_server.server's default
# ``identity_storage_path`` and the Docker volume mount used by
# tools/install_services.run_pi_server().
DEFAULT_IDENTITY_DIR = os.path.expanduser("~/.local/share/lmao_server/lxmf")

_IDENTITY_FILENAME = "identity"


def _require_rns():
    if RNS is None:
        raise ImportError(
            "Reticulum (RNS) is not installed. Server identity management "
            "is unavailable. Install with: pip install rns"
        )


def identity_file_path(identity_dir: str | None = None) -> str:
    """Return the full path of the server identity file."""
    return os.path.join(identity_dir or DEFAULT_IDENTITY_DIR, _IDENTITY_FILENAME)


def ensure_server_identity(identity_dir: str | None = None):
    """Load the persisted server identity, creating it when missing.

    Parameters
    ----------
    identity_dir:
        Directory holding the ``identity`` file.  Defaults to
        :data:`DEFAULT_IDENTITY_DIR`.  Created (with parents) when a new
        identity must be saved.

    Returns
    -------
    ``(identity, identity_file)`` — the ``RNS.Identity`` instance and the
    path it was loaded from / saved to.

    Raises
    ------
    ImportError
        When RNS is not installed.
    OSError
        When the identity file cannot be written.
    """
    _require_rns()

    identity_file = identity_file_path(identity_dir)

    if os.path.isfile(identity_file):
        try:
            identity = RNS.Identity.from_file(identity_file)
            if identity is not None:
                _logger.info("Loaded server identity from %s", identity_file)
                return identity, identity_file
        except (ValueError, KeyError, IOError, OSError) as exc:
            _logger.warning(
                "Could not load identity from %s (%s) — creating a new one",
                identity_file,
                exc,
            )

    identity = RNS.Identity()
    os.makedirs(os.path.dirname(identity_file), exist_ok=True)
    identity.to_file(identity_file)
    _logger.info("Created new server identity, saved to %s", identity_file)
    return identity, identity_file


def delivery_destination_hash_hex(identity) -> str:
    """Compute the server's ``lxmf.delivery`` destination hash as hex.

    This is the value Cardputer clients must set as ``DEST_HASH`` in
    ``config.py``.  It is deterministic from the identity, so tooling can
    compute it without a running server.

    Parameters
    ----------
    identity:
        An ``RNS.Identity`` instance (e.g. from
        :func:`ensure_server_identity`).

    Returns
    -------
    32-character lowercase hex string (16-byte destination hash).
    """
    _require_rns()

    dest = RNS.Destination(
        identity,
        RNS.Destination.OUT,
        RNS.Destination.SINGLE,
        "lxmf",
        "delivery",
    )
    return RNS.hexrep(dest.hash, delimit=False)


def ensure_delivery_destination_hash(identity_dir: str | None = None) -> str:
    """Convenience: ensure the identity exists and return its delivery hash.

    Combines :func:`ensure_server_identity` and
    :func:`delivery_destination_hash_hex` for install tooling.
    """
    identity, _ = ensure_server_identity(identity_dir)
    return delivery_destination_hash_hex(identity)
