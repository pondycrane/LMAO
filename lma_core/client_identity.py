"""Client identity persistence + LXMF delivery destination hash helpers.

The Cardputer LMAO client mints a fresh Reticulum identity on first boot and
persists it on the device (``/flash/rns/identity`` on M5Stack MicroPython;
NVS for the native firmware).  A full device re-flash erases that storage and
mints a *new* identity, whose ``lxmf/delivery`` hash no longer appears in the
server's ``ALLOWED_CLIENTS`` — the exact drift that forces a re-allow-list
after every field flash (observed 2026-09-27: reflashed Cardputer came up as
``99ce3231…``, different from the allow-listed native/µReticulum hashes).

This module is the "no drift" counterpart of :mod:`lma_core.server_identity`:
a single canonical client identity is kept on the *host* at
``~/.local/share/lmao_client/lxmf/identity`` (standard RNS identity file
format — the same format µReticulum writes and reference RNS reads, verified
to round-trip to the identical ``lxmf.delivery`` hash).  Install tooling:

  1. ``adopt_client_identity_bytes`` / ``ensure_client_identity`` — seed the
     canonical store.  On the first install after this change it **adopts the
     on-device identity** (so the currently allow-listed hash keeps working);
     a brand-new device mints a fresh canonical identity on the host.
  2. The canonical file is then written onto every Cardputer at flash time
     (``/flash/rns/identity``) and, for the native firmware, its 64-byte
     private key is baked into the build (``LMAO_NODE_IDENTITY_HEX``) — so
     re-flashing can never change the delivery hash again.

RNS is imported lazily via :mod:`lma_core.rns_di` so this module can be
imported without Reticulum installed; the public functions raise
``ImportError`` with a descriptive message when RNS is unavailable.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile

from lma_core.rns_di import RNS
from lma_core.server_identity import delivery_destination_hash_hex

_logger = logging.getLogger(__name__)

# Canonical client identity directory.  Deliberately separate from the server
# identity store (lma_core/server_identity.DEFAULT_IDENTITY_DIR): this is the
# device identity that ends up in /flash/rns/identity, not the server's.
DEFAULT_IDENTITY_DIR = os.path.expanduser("~/.local/share/lmao_client/lxmf")

_IDENTITY_FILENAME = "identity"


def _require_rns():
    if RNS is None:
        raise ImportError(
            "Reticulum (RNS) is not installed. Client identity management "
            "is unavailable. Install with: pip install rns"
        )


def identity_file_path(identity_dir: str | None = None) -> str:
    """Return the full path of the canonical client identity file."""
    return os.path.join(identity_dir or DEFAULT_IDENTITY_DIR, _IDENTITY_FILENAME)


def _load_identity_from_bytes(data: bytes):
    """Parse *data* as a standard RNS identity file, or return *None*.

    Reference RNS only parses identity files from a path, so the bytes are
    staged in a temp file.  Returns an ``RNS.Identity`` when *data* is a
    valid identity file, ``None`` otherwise.
    """
    _require_rns()
    try:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        try:
            return RNS.Identity.from_file(tmp_path)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
    except (ValueError, KeyError, OSError):
        return None


def ensure_client_identity(identity_dir: str | None = None):
    """Load the canonical client identity, creating it when missing.

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
                _logger.info("Loaded canonical client identity from %s", identity_file)
                return identity, identity_file
        except (ValueError, KeyError, OSError) as exc:
            _logger.warning(
                "Could not load identity from %s (%s) — creating a new one",
                identity_file,
                exc,
            )

    identity = RNS.Identity()
    os.makedirs(os.path.dirname(identity_file), exist_ok=True)
    identity.to_file(identity_file)
    _logger.info("Created new canonical client identity, saved to %s", identity_file)
    return identity, identity_file


def adopt_client_identity_bytes(data: bytes, identity_dir: str | None = None):
    """Persist an existing on-device identity as the canonical client identity.

    Used on the first install after this change so the currently allow-listed
    device hash keeps working instead of being silently replaced by a fresh
    canonical mint.  *data* must be a valid standard RNS identity file (the
    format µReticulum writes to ``/flash/rns/identity``); invalid data is
    rejected rather than overwriting a valid canonical store.

    Parameters
    ----------
    data:
        Raw bytes of an on-device RNS identity file.

    Returns
    -------
    ``(identity, identity_file)`` when *data* was adopted, ``None`` when it
    did not parse as an RNS identity file.
    """
    identity = _load_identity_from_bytes(data)
    if identity is None:
        _logger.warning("Refusing to adopt invalid identity bytes (%d B)", len(data))
        return None

    identity_file = identity_file_path(identity_dir)
    os.makedirs(os.path.dirname(identity_file), exist_ok=True)
    with open(identity_file, "wb") as f:
        f.write(data)
    _logger.info(
        "Adopted on-device client identity as canonical (%s) — hash %s",
        identity_file,
        delivery_destination_hash_hex(identity),
    )
    return identity, identity_file


def ensure_client_delivery_destination_hash(identity_dir: str | None = None) -> str:
    """Convenience: ensure the canonical identity exists and return its delivery hash.

    This is the value that must appear in the server's ``ALLOWED_CLIENTS`` /
    ``LMAO_ALLOWED_CLIENTS`` — the same hash install_all pins onto the device.
    """
    identity, _ = ensure_client_identity(identity_dir)
    return delivery_destination_hash_hex(identity)
