"""Tests for lma_core/contact_client.py — the client side of the contact book.

The API is mocked at the urllib boundary: these pin the payload shape a
provisioning run sends and the two failure modes that matter on a flash —
an unreachable API (non-fatal) and a name already owned by another device
(register under a placeholder, never steal the row).
"""

from __future__ import annotations

import json
import sys
import urllib.error

import pytest

from lma_core import contact_client


class _Resp:
    def __init__(self, status=201):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture(calls, status=201, fail=None):
    """Fake urlopen that records each request body."""

    def _urlopen(req, timeout=None):
        calls.append(json.loads(req.data.decode()))
        if fail is not None:
            raise fail
        return _Resp(status)

    return _urlopen


def test_register_posts_an_approved_record(monkeypatch):
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", _capture(calls))
    assert contact_client.register_contact(
        "study room", "95b658d94028bdecec39b7098985d2de",
        "robodog_home", "aa" * 64,
    ) is True
    assert calls == [{
        "delivery_hash": "95b658d94028bdecec39b7098985d2de",
        "name": "study room",
        "type": "robodog_home",
        "pubkey_hex": "aa" * 64,
        "state": "approved",
    }]


def test_an_unreachable_api_is_not_fatal(monkeypatch):
    calls = []
    monkeypatch.setattr("urllib.request.urlopen",
                        _capture(calls, fail=OSError("refused")))
    assert contact_client.register_contact("kitchen", "a" * 32) is False
    assert len(calls) == 1


def test_a_taken_name_falls_back_to_a_placeholder(monkeypatch):
    """409 means another device owns the name: re-post nameless so the board
    is still addressable + approved, instead of being left pending."""
    calls = []

    def _urlopen(req, timeout=None):
        calls.append(json.loads(req.data.decode()))
        if "name" in calls[-1]:
            raise urllib.error.HTTPError(
                "url", 409, "conflict", None, None
            )
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    assert contact_client.register_contact(
        "study room", "b" * 32, "robodog_home"
    ) is True
    assert len(calls) == 2
    assert calls[1] == {
        "delivery_hash": "b" * 32,
        "type": "robodog_home",
        "pubkey_hex": None,
        "state": "approved",
    }


def test_other_http_errors_are_reported_as_failures(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _capture([], fail=urllib.error.HTTPError("url", 400, "bad", None, None)),
    )
    assert contact_client.register_contact("kitchen", "a" * 32) is False


def test_lookup_tries_name_then_hash_then_type(monkeypatch):
    seen = []

    class _LookupResp(_Resp):
        def __init__(self, contacts):
            super().__init__(200)
            self._contacts = contacts

        def read(self):
            return json.dumps({"contacts": self._contacts}).encode()

    def _urlopen(url, timeout=None):
        seen.append(url)
        return _LookupResp([{"delivery_hash": "c" * 32}]
                           if "hash=" in url else [])

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    assert contact_client.lookup_contact("kitchen")["delivery_hash"] == "c" * 32
    assert [u.split("?")[1].split("=")[0] for u in seen] == ["name", "hash"]


def test_lookup_refuses_an_ambiguous_match(monkeypatch):
    class _LookupResp(_Resp):
        def read(self):
            return json.dumps({"contacts": [{"delivery_hash": "c" * 32},
                                            {"delivery_hash": "d" * 32}]}).encode()

    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: _LookupResp())
    assert contact_client.lookup_contact("cardputer") is None


def test_a_revoked_contact_is_not_addressable(monkeypatch):
    """Revoking a device must stop sends to it: the resolver treats the
    revoked row as no match instead of handing out its hash."""
    class _LookupResp(_Resp):
        def read(self):
            return json.dumps({"contacts": [{"delivery_hash": "e" * 32,
                                             "state": "revoked"}]}).encode()

    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: _LookupResp())
    assert contact_client.lookup_contact("kitchen") is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
