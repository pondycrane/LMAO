"""Unit tests for the controller's directive publisher."""

import json
import urllib.error
import urllib.request

from lmao_socket_humidifier.directive import Directive, Publisher


def test_off_payload_carries_no_lease():
    d = Directive(key="humidifier", relay_on=False, reason="in band", lease_s=300, issued_s=1.0)
    assert d.payload()["lease_s"] == 0
    assert d.payload()["on"] is False


def test_on_payload_keeps_the_lease_and_rounds_the_stamp():
    d = Directive(
        key="humidifier", relay_on=True, reason="dry", lease_s=300, issued_s=1763700000.123456
    )
    body = d.payload()
    assert body["on"] is True
    assert body["lease_s"] == 300
    assert body["issued_s"] == 1763700000.123


class _Capture:
    """Stand-in for urlopen that records the request."""

    def __init__(self):
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append((req, timeout))
        return _Response()


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b"{}"


def test_publish_posts_to_the_keyed_url(monkeypatch):
    capture = _Capture()
    monkeypatch.setattr(urllib.request, "urlopen", capture)
    pub = Publisher("http://localhost:8082/humidifier/")
    ok = pub.publish(
        Directive(key="humidifier-baby", relay_on=True, reason="dry", lease_s=300, issued_s=1.0)
    )
    assert ok is True
    assert pub.last_ok_s > 0
    req, timeout = capture.requests[0]
    assert req.full_url == "http://localhost:8082/humidifier/humidifier-baby"
    assert req.get_method() == "POST"
    assert json.loads(req.data) == {
        "on": True,
        "lease_s": 300,
        "reason": "dry",
        "issued_s": 1.0,
        "fail_safe": False,
    }
    assert timeout == 5.0


def test_key_is_url_quoted(monkeypatch):
    capture = _Capture()
    monkeypatch.setattr(urllib.request, "urlopen", capture)
    Publisher("http://localhost:8082/humidifier").publish(
        Directive(key="baby room", relay_on=False, reason="off", lease_s=0, issued_s=1.0)
    )
    assert capture.requests[0][0].full_url.endswith("/humidifier/baby%20room")


def test_publish_failure_is_reported_once(monkeypatch, caplog):
    calls = []

    def boom(req, timeout=None):
        calls.append(req)
        raise urllib.error.URLError("No route to host")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    pub = Publisher("http://localhost:8082/humidifier")
    d = Directive(key="humidifier", relay_on=True, reason="dry", lease_s=300, issued_s=1.0)
    assert pub.publish(d) is False
    assert pub.publish(d) is False
    assert len(calls) == 2
    # The same failure must not be logged twice per cycle.
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1


def test_publish_recovery_clears_the_error(monkeypatch):
    state = {"fail": True}

    def maybe(req, timeout=None):
        if state["fail"]:
            raise urllib.error.URLError("down")
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", maybe)
    pub = Publisher("http://localhost:8082/humidifier")
    d = Directive(key="humidifier", relay_on=True, reason="dry", lease_s=300, issued_s=1.0)
    assert pub.publish(d) is False
    assert pub._last_error is not None
    state["fail"] = False
    assert pub.publish(d) is True
    assert pub._last_error is None
