"""Host tests for the central contact book (issue #151).

The book is the server's receiver directory: it knows which devices are on the
LMAO network (learned as they report) and lets clients resolve a receiver by
name/type/hash.  These tests cover the SQLite store and the aiohttp API.
"""

import os
import sys

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


def test_auto_learn_does_not_downgrade_an_operator_set_type(tmp_path):
    """The learn path always reports type 'device'; it must not erase a real
    type the operator set via POST /contacts."""
    b = _book(tmp_path)
    b.register(
        delivery_hash="81aae19de81066b491f437d0e9820618",
        device_type="robodog_home",
        device_name="main bedroom",
        name_source="operator",
    )
    b.register(
        delivery_hash="81aae19de81066b491f437d0e9820618",
        device_type="device",
        device_name="main bedroom",
        name_source="device",
    )
    e = b.find(delivery_hash="81aae19de81066b491f437d0e9820618")[0]
    assert e["device_type"] == "robodog_home"
    assert e["device_name"] == "main bedroom"
    assert e["name_source"] == "operator"
    b.close()


def test_operator_rename_survives_every_later_report(tmp_path):
    """A device reports its own node_id on EVERY report; an operator rename
    must not be reverted by the next one."""
    b = _book(tmp_path)
    h = "983c04117aea3d4ca8f48962355624fc"
    b.register(delivery_hash=h, device_type="device",
               device_name="main bedroom", name_source="device")
    b.register(delivery_hash=h, device_type="robodog_home",
               device_name="Kitchen", name_source="operator")
    for _ in range(3):
        b.register(delivery_hash=h, device_type="device",
                   device_name="main bedroom", name_source="device")
    e = b.find(delivery_hash=h)[0]
    assert e["device_name"] == "Kitchen"
    assert e["device_type"] == "robodog_home"
    b.close()


def test_device_rename_is_followed(tmp_path):
    """A device that changes its own reported name updates the contact."""
    b = _book(tmp_path)
    h = "983c04117aea3d4ca8f48962355624fc"
    b.register(delivery_hash=h, device_type="device",
               device_name="main bedroom", name_source="device")
    b.register(delivery_hash=h, device_type="device",
               device_name="guest room", name_source="device")
    e = b.find(delivery_hash=h)[0]
    assert e["device_name"] == "guest room"
    assert e["name_source"] == "device"
    b.close()


def test_placeholder_refresh_does_not_erase_a_device_name(tmp_path):
    b = _book(tmp_path)
    h = "983c04117aea3d4ca8f48962355624fc"
    b.register(delivery_hash=h, device_type="device",
               device_name="main bedroom", name_source="device")
    b.register(delivery_hash=h, device_type="device", name_source="auto")
    e = b.find(delivery_hash=h)[0]
    assert e["device_name"] == "main bedroom"
    b.close()


def test_legacy_table_migrates_custom_names_to_operator_owned(tmp_path):
    """A contacts table created before name ownership existed must keep its
    hand-set names when the learn path next reports."""
    import sqlite3

    db = os.path.join(tmp_path, "contacts.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE contacts (device_name TEXT PRIMARY KEY, device_type TEXT NOT NULL,"
        " delivery_hash TEXT NOT NULL UNIQUE, pubkey_hex TEXT, identity_hash TEXT,"
        " first_seen REAL NOT NULL, last_seen REAL NOT NULL)"
    )
    conn.execute(
        "INSERT INTO contacts VALUES ('Kitchen','robodog_home','983c04117aea3d4ca8f48962355624fc',"
        "NULL,NULL,1.0,1.0)"
    )
    conn.execute(
        "INSERT INTO contacts VALUES ('cardputer-0618','cardputer','81aae19de81066b491f437d0e9820618',"
        "NULL,NULL,1.0,1.0)"
    )
    conn.commit()
    conn.close()

    b = ContactBook(db)
    assert b.find(device_name="Kitchen")[0]["name_source"] == "operator"
    assert b.find(device_name="cardputer-0618")[0]["name_source"] == "auto"

    b.register(delivery_hash="983c04117aea3d4ca8f48962355624fc",
               device_type="device", device_name="main bedroom",
               name_source="device")
    assert b.find(device_name="Kitchen"), "operator name must survive the report"
    b.close()


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__] + sys.argv[1:]))
