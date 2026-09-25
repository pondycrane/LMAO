"""Host tests for the central contact book (issue #151).

The book is the server's receiver directory: it knows which devices are on the
LMAO network (learned as they report) and lets clients resolve a receiver by
name/type/hash.  These tests cover the SQLite store and the aiohttp API.
"""

import os

from lma_core.contact_book import ContactBook
from lma_core.contacts_api import create_app


def _book(tmp_path):
    return ContactBook(os.path.join(tmp_path, "contacts.db"))


# ── Store ──────────────────────────────────────────────────────────────────

def test_register_and_known(tmp_path):
    b = _book(tmp_path)
    assert not b.is_known("81aae19de81066b491f437d0e9820618")
    b.register(delivery_hash="81aae19de81066b491f437d0e9820618", device_type="cardputer")
    assert b.is_known("81aae19de81066b491f437d0e9820618")
    entry = b.find(delivery_hash="81aae19de81066b491f437d0e9820618")[0]
    # Un-named auto-contact: type + short-hash suffix so it is addressable.
    assert entry["device_name"] == "cardputer-0618"
    assert entry["device_type"] == "cardputer"
    b.close()


def test_register_renames_and_keeps_pubkey(tmp_path):
    b = _book(tmp_path)
    b.register(
        delivery_hash="81aae19de81066b491f437d0e9820618",
        device_type="device",
        pubkey_hex="aa11",
    )
    # Operator sets the friendly name + real type; pubkey is coalesced (kept).
    b.register(
        delivery_hash="81aae19de81066b491f437d0e9820618",
        device_type="cardputer",
        device_name="living-room",
        pubkey_hex=None,
    )
    e = b.find(delivery_hash="81aae19de81066b491f437d0e9820618")[0]
    assert e["device_name"] == "living-room"
    assert e["device_type"] == "cardputer"
    assert e["pubkey_hex"] == "aa11"
    b.close()


def test_touch_only_known(tmp_path):
    b = _book(tmp_path)
    assert b.touch("81aae19de81066b491f437d0e9820618") is False
    b.register(
        delivery_hash="81aae19de81066b491f437d0e9820618",
        device_type="cardputer",
        device_name="c",
    )
    assert b.touch("81aae19de81066b491f437d0e9820618") is True
    b.close()


def test_find_by_name_and_type(tmp_path):
    b = _book(tmp_path)
    b.register(delivery_hash="a" * 32, device_type="cardputer", device_name="living")
    b.register(delivery_hash="b" * 32, device_type="sprout", device_name="backyard")
    assert len(b.find(device_type="cardputer")) == 1
    assert b.find(device_name="living")[0]["device_type"] == "cardputer"
    assert len(b.find()) == 2
    assert len(b.find(device_name="nope")) == 0
    b.close()


# ── API ────────────────────────────────────────────────────────────────────

def test_api_register_and_find(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    b = _book(tmp_path)

    async def run():
        async with TestClient(TestServer(create_app(b))) as cli:
            # Empty directory.
            r = await cli.get("/contacts")
            assert r.status == 200
            assert (await r.json())["contacts"] == []

            # Register a receiver.
            r = await cli.post(
                "/contacts",
                json={
                    "delivery_hash": "81aae19de81066b491f437d0e9820618",
                    "type": "cardputer",
                    "name": "living-room",
                },
            )
            assert r.status == 201

            # Find by name — the receiver-resolution path for clients.
            r = await cli.get("/contacts/find", params={"name": "living-room"})
            assert r.status == 200
            found = (await r.json())["contacts"]
            assert len(found) == 1
            assert found[0]["delivery_hash"] == "81aae19de81066b491f437d0e9820618"
            assert found[0]["device_type"] == "cardputer"

            # Find by type.
            r = await cli.get("/contacts/find", params={"type": "cardputer"})
            assert len((await r.json())["contacts"]) == 1

            # Find by delivery hash.
            r = await cli.get(
                "/contacts/find", params={"hash": "81aae19de81066b491f437d0e9820618"}
            )
            assert len((await r.json())["contacts"]) == 1

            # Registration requires both fields.
            r = await cli.post("/contacts", json={"type": "sprout"})
            assert r.status == 400

    import asyncio

    asyncio.run(run())
