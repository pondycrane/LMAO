"""Client side of the LMAO contact book (the server's receiver directory).

One place for the HTTP calls that every client-side tool makes against the
Contacts API (``:8081``): publishing a device's public record at provisioning
time, and resolving a receiver name/type/hash before a send.  Keeping them
here means ``install_all``, ``device_provision`` and the human client share
one URL convention, one payload shape and one error policy.

The API base is ``LMAO_CONTACTS_API`` (default ``http://127.0.0.1:8081``);
off-node flashes/builds point it at a reachable address (a kubectl
port-forward, or the cluster host).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_logger = logging.getLogger(__name__)


def contacts_api_base() -> str:
    """Base URL of the server's contact-book HTTP API."""
    return os.environ.get("LMAO_CONTACTS_API", "http://127.0.0.1:8081").rstrip("/")


def _post_contact(body: dict[str, Any]) -> int:
    """POST one contact record; returns the HTTP status (raises on no reply)."""
    req = urllib.request.Request(
        f"{contacts_api_base()}/contacts",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


def register_contact(
    device_name: str,
    delivery_hash: str,
    device_type: str = "cardputer",
    pubkey_hex: str | None = None,
    state: str = "approved",
) -> bool:
    """Publish a device's PUBLIC record (never its private key) to the book.

    A provisioning run with the device physically present is the operator's
    trust decision, so the record is registered ``approved`` by default — no
    manifest edit and no rollout.  Returns True on success; an unreachable API
    is logged, not raised (the caller prints the hash and the
    ``POST /contacts/<hash>/approve`` URL instead — the env seed cannot
    approve a device the server has already seen).

    A **name already owned by another device** is not stolen: the API answers
    409, and the record is re-posted without a name so the device is still
    addressable (and approved) under its ``<type>-<last4>`` placeholder — a
    failed registration would otherwise leave a freshly flashed board pending
    and silently dropped by the server gate.
    """
    delivery_hash = delivery_hash.lower()
    body: dict[str, Any] = {
        "delivery_hash": delivery_hash,
        "name": device_name,
        "type": device_type,
        "pubkey_hex": pubkey_hex,
        "state": state,
    }
    try:
        status = _post_contact(body)
    except urllib.error.HTTPError as exc:
        if exc.code != 409:
            _logger.warning("Contact registration failed (HTTP %s): %s", exc.code, exc)
            return False
        _logger.warning(
            "Contact name %r is already taken by another device — registering "
            "%s under a placeholder instead; rename it with POST /contacts.",
            device_name, delivery_hash[:12],
        )
        body.pop("name")
        try:
            status = _post_contact(body)
        except Exception as exc2:
            _logger.warning("Contact registration failed (%s): %s", exc2, exc2)
            return False
    except Exception as exc:
        _logger.warning("Contact registration failed: %s", exc)
        return False

    _logger.info(
        "Registered contact %s -> %s (%s, HTTP %s)",
        device_name, delivery_hash[:12], state, status,
    )
    return True


def lookup_contact(token: str) -> dict[str, Any] | None:
    """Resolve a receiver by name, delivery hash, or device type.

    Returns the single matching contact dict, or None when the API is
    unreachable or nothing matches.  An ambiguous match (a type shared by
    several devices) is refused: the caller must name one.  A ``revoked``
    contact is not addressable and counts as no match.
    """
    base = contacts_api_base()
    for key in ("name", "hash", "type"):
        url = f"{base}/contacts/find?{urllib.parse.urlencode({key: token})}"
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                contacts = json.load(resp).get("contacts") or []
        except Exception as exc:
            _logger.debug("Contact lookup failed (%s): %s", url, exc)
            return None
        contacts = [c for c in contacts if c.get("state") != "revoked"]
        if contacts:
            if len(contacts) > 1:
                _logger.warning(
                    "%r matches %d contacts — use a name or hash", token, len(contacts)
                )
                return None
            return contacts[0]
    return None
