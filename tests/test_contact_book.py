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
    # A learned device starts pending — the operator's approval is the gate.
    assert entry["state"] == "pending"
    assert b.is_approved("81aae19de81066b491f437d0e9820618") is False
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


# ── Lifecycle: the contact book is the ONE registry ─────────────────────────

def test_approve_and_revoke_gate_the_registry(tmp_path):
    b = _book(tmp_path)
    h = "81aae19de81066b491f437d0e9820618"
    b.register(delivery_hash=h, device_type="cardputer")
    assert b.state_of(h) == "pending"
    assert b.set_state(h, "approved") is True
    assert b.is_approved(h) is True
    assert b.find(delivery_hash=h)[0]["approved_at"] is not None
    assert b.set_state(h, "revoked") is True
    assert b.is_approved(h) is False
    assert b.set_state("f" * 32, "approved") is False
    b.close()


def test_reports_do_not_change_an_operator_state(tmp_path):
    """The learn path upserts on every report; it must not flip a revoked
    device back to pending/approved."""
    b = _book(tmp_path)
    h = "81aae19de81066b491f437d0e9820618"
    b.register(delivery_hash=h, device_type="cardputer", state="approved")
    b.register(delivery_hash=h, device_type="device",
               device_name="kitchen", name_source="device")
    assert b.state_of(h) == "approved"
    b.close()


def test_seed_approved_is_insert_only(tmp_path):
    """LMAO_ALLOWED_CLIENTS is a cold-start seed: re-seeding must not undo a
    revocation, and must not rewrite an existing row's name."""
    b = _book(tmp_path)
    h = "81aae19de81066b491f437d0e9820618"
    assert b.seed_approved(h) is True
    assert b.is_approved(h) is True
    b.register(delivery_hash=h, device_type="cardputer",
               device_name="kitchen", name_source="operator")
    b.set_state(h, "revoked")
    assert b.seed_approved(h) is False
    e = b.find(delivery_hash=h)[0]
    assert e["state"] == "revoked"
    assert e["device_name"] == "kitchen"
    b.close()


def test_api_approve_and_revoke(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    b = _book(tmp_path)
    h = "81aae19de81066b491f437d0e9820618"

    async def run():
        async with TestClient(TestServer(create_app(b))) as cli:
            r = await cli.post("/contacts", json={"delivery_hash": h, "type": "sprout",
                                                  "state": "pending"})
            assert r.status == 201
            assert (await r.json())["contact"]["state"] == "pending"

            r = await cli.get("/contacts", params={"state": "pending"})
            assert len((await r.json())["contacts"]) == 1

            r = await cli.post(f"/contacts/{h}/approve")
            assert r.status == 200
            assert (await r.json())["contact"]["state"] == "approved"

            r = await cli.post(f"/contacts/{h}/revoke")
            assert r.status == 200
            assert (await r.json())["contact"]["state"] == "revoked"

            # Unknown hash is a 404, not a silent success.
            r = await cli.post(f"/contacts/{'f' * 32}/approve")
            assert r.status == 404

            # A POST without state is the operator's approval (install_all path).
            h2 = "a" * 32
            r = await cli.post("/contacts", json={"delivery_hash": h2, "type": "cardputer",
                                                  "name": "kitchen"})
            assert r.status == 201
            assert (await r.json())["contact"]["state"] == "approved"

    import asyncio

    asyncio.run(run())
    b.close()


def test_a_device_reporting_a_taken_name_falls_back_to_its_placeholder(tmp_path):
    """Two devices claiming one name must both keep a row: the report path
    falls back to <type>-<last4> (never fails every report)."""
    b = _book(tmp_path)
    b.register(delivery_hash="a" * 32, device_type="cardputer",
               device_name="kitchen", name_source="operator")
    e = b.register(delivery_hash="b" * 32, device_type="device",
                   device_name="kitchen", name_source="device")
    assert e["device_name"] == "device-" + "b" * 4
    assert e["state"] == "pending"
    assert b.find(device_name="kitchen")[0]["delivery_hash"] == "a" * 32
    b.close()


def test_api_duplicate_name_is_a_conflict(tmp_path):
    """device_name is a unique attribute, not the key: registering a second
    device under an existing name must be rejected, never re-point the row."""
    from aiohttp.test_utils import TestClient, TestServer

    b = _book(tmp_path)

    async def run():
        async with TestClient(TestServer(create_app(b))) as cli:
            r = await cli.post("/contacts", json={"delivery_hash": "a" * 32,
                                                  "type": "cardputer", "name": "kitchen"})
            assert r.status == 201
            r = await cli.post("/contacts", json={"delivery_hash": "b" * 32,
                                                  "type": "sprout", "name": "kitchen"})
            assert r.status == 409
            assert len((await (await cli.get("/contacts")).json())["contacts"]) == 1

    import asyncio

    asyncio.run(run())
    b.close()


def test_legacy_pk_on_name_migrates_to_delivery_hash(tmp_path):
    """A pre-lifecycle book keyed on device_name must migrate in place:
    delivery_hash becomes the PK, legacy rows become approved, and the dead
    identity_hash column is gone."""
    import sqlite3

    db = os.path.join(tmp_path, "contacts.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE contacts (device_name TEXT PRIMARY KEY, device_type TEXT NOT NULL,"
        " delivery_hash TEXT NOT NULL UNIQUE, pubkey_hex TEXT, identity_hash TEXT,"
        " first_seen REAL NOT NULL, last_seen REAL NOT NULL,"
        " name_source TEXT NOT NULL DEFAULT 'auto')"
    )
    conn.execute(
        "INSERT INTO contacts VALUES ('Kitchen','robodog_home',"
        "'983c04117aea3d4ca8f48962355624fc',NULL,NULL,1.0,2.0,'operator')"
    )
    conn.commit()
    conn.close()

    b = ContactBook(db)
    e = b.find(device_name="Kitchen")[0]
    assert e["state"] == "approved"
    assert e["approved_at"] == 2.0
    columns = {
        row["name"] for row in b._conn.execute("PRAGMA table_info(contacts)")
    }
    assert "identity_hash" not in columns
    info = {row["name"]: row["pk"] for row in b._conn.execute("PRAGMA table_info(contacts)")}
    assert info["delivery_hash"] == 1
    assert info["device_name"] == 0

    # The migrated row keeps its operator ownership across later reports.
    b.register(delivery_hash="983c04117aea3d4ca8f48962355624fc",
               device_type="device", device_name="main bedroom",
               name_source="device")
    assert b.find(device_name="Kitchen")[0]["state"] == "approved"
    b.close()


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__] + sys.argv[1:]))
