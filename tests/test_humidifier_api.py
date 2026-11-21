"""Unit tests for lma_core.humidifier_api (the directive relay)."""

import pytest

from lma_core.humidifier_api import DirectiveStore


def _payload(**over):
    base = {"on": True, "reason": "dew 9.4C <= 9.5C", "lease_s": 300, "issued_s": 1000.0}
    base.update(over)
    return base


# ── store: validation ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "payload",
    [
        {"reason": "no on key", "lease_s": 300},
        {"on": "yes", "lease_s": 300},
        {"on": True, "lease_s": "300"},
        {"on": True, "lease_s": -1},
        {"on": True, "lease_s": 3601},
        {"on": True, "lease_s": 0},  # an ON directive must carry a lease
        {"on": False, "lease_s": 0, "reason": 7},
    ],
)
def test_bad_payloads_are_rejected(payload):
    with pytest.raises(ValueError):
        DirectiveStore().publish("humidifier", payload)


def test_key_must_be_a_bounded_nonempty_string():
    store = DirectiveStore()
    with pytest.raises(ValueError):
        store.publish("", _payload())
    with pytest.raises(ValueError):
        store.publish("x" * 65, _payload())


# ── store: staleness ─────────────────────────────────────────────────


def test_fresh_directive_is_served_with_its_age():
    clock = lambda: 1100.0  # noqa: E731
    store = DirectiveStore(max_age_s=180, clock=clock)
    store.publish("humidifier", _payload(issued_s=1000.0))
    item = store.get("humidifier")
    assert item is not None
    assert item["on"] is True
    assert item["lease_s"] == 300
    assert item["age_s"] == 100.0


def test_directive_older_than_max_age_is_not_served():
    clock = lambda: 1200.0  # noqa: E731
    store = DirectiveStore(max_age_s=180, clock=clock)
    store.publish("humidifier", _payload(issued_s=1000.0))
    assert store.get("humidifier") is None


def test_unknown_key_is_none():
    assert DirectiveStore().get("nope") is None


def test_publish_replaces_the_previous_decision():
    clock = lambda: 1000.0  # noqa: E731
    store = DirectiveStore(clock=clock)
    store.publish("humidifier", _payload(on=True, lease_s=300))
    store.publish("humidifier", _payload(on=False, lease_s=0, reason="in band"))
    item = store.get("humidifier")
    assert item["on"] is False
    assert item["lease_s"] == 0
    assert item["reason"] == "in band"


def test_reason_is_truncated():
    store = DirectiveStore()
    store.publish("humidifier", _payload(reason="x" * 500))
    assert len(store.get("humidifier")["reason"]) == 200


def test_issued_s_defaults_to_the_store_clock():
    clock = lambda: 4242.0  # noqa: E731
    store = DirectiveStore(clock=clock)
    store.publish("humidifier", {"on": False, "lease_s": 0})
    assert store.get("humidifier")["issued_s"] == 4242.0


# ── HTTP app ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_is_503_before_any_publish():
    from aiohttp.test_utils import TestClient, TestServer

    from lma_core.humidifier_api import create_app

    async with TestClient(TestServer(create_app(DirectiveStore()))) as cli:
        resp = await cli.get("/humidifier/humidifier")
        assert resp.status == 503


@pytest.mark.asyncio
async def test_publish_then_get_round_trips():
    from aiohttp.test_utils import TestClient, TestServer

    from lma_core.humidifier_api import create_app

    store = DirectiveStore()
    async with TestClient(TestServer(create_app(store))) as cli:
        resp = await cli.post(
            "/humidifier/humidifier-baby",
            json={"on": True, "reason": "dew 9.4C <= 9.5C", "lease_s": 300},
        )
        assert resp.status == 201
        resp = await cli.get("/humidifier/humidifier-baby")
        assert resp.status == 200
        body = await resp.json()
        assert body["on"] is True
        assert body["lease_s"] == 300
        assert body["reason"] == "dew 9.4C <= 9.5C"
        assert body["age_s"] >= 0


@pytest.mark.asyncio
async def test_bad_publish_body_is_400():
    from aiohttp.test_utils import TestClient, TestServer

    from lma_core.humidifier_api import create_app

    async with TestClient(TestServer(create_app(DirectiveStore()))) as cli:
        resp = await cli.post("/humidifier/k", json={"on": True, "lease_s": 0})
        assert resp.status == 400


@pytest.mark.asyncio
async def test_healthz_lists_published_keys():
    from aiohttp.test_utils import TestClient, TestServer

    from lma_core.humidifier_api import create_app

    store = DirectiveStore()
    store.publish("humidifier", _payload())
    async with TestClient(TestServer(create_app(store))) as cli:
        body = await (await cli.get("/healthz")).json()
        assert body["ok"] is True
        assert body["keys"] == ["humidifier"]


# ── publish trust domain: loopback only ──────────────────────────────


@pytest.mark.parametrize(
    "peer, allowed",
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("::ffff:127.0.0.1", True),  # IPv4-mapped, what aiohttp reports on dual stack
        ("192.168.50.21", False),
        ("192.168.10.19", False),
        ("::ffff:192.168.50.21", False),
        ("10.0.0.1", False),
        ("", False),
        (None, False),
    ],
)
def test_publish_is_loopback_only(peer, allowed):
    from lma_core.humidifier_api import _is_loopback

    assert _is_loopback(peer) is allowed
