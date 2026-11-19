"""Controller glue tests: humidity extraction + decision application.

The Reticulum client is faked here (``request_env`` returns the same summary
dict ``summarize_chart`` produces), so these exercise the controller's own
logic without a mesh.
"""

import pytest

from lmao_socket_humidifier import controller
from lmao_socket_humidifier.control import Decision, Setpoints
from lmao_socket_humidifier.socket_client import SocketError

SP = Setpoints(lease_s=300)


class FakeReader:
    def __init__(self, summary):
        self.summary = summary
        self.calls = []

    def request_env(self, count=0, since_ms=0, node_id=None):
        self.calls.append({"count": count, "node_id": node_id})
        return self.summary


class FakeSocket:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def set_on(self, lease_s):
        self.calls.append(("on", lease_s))
        if self.fail:
            raise SocketError("boom")

    def set_off(self):
        self.calls.append(("off", 0))
        if self.fail:
            raise SocketError("boom")


def test_reading_pins_the_env_node_and_derives_sample_age():
    reader = FakeReader(
        {
            "latest_humidity_pct": 37,
            "start_ms": 1_700_000_000_000,
            "node_id": "main bed",
        }
    )
    hum, age = controller.read_humidity(reader, "main bedroom", 1_700_000_012.0)
    assert hum == 37
    assert age == pytest.approx(12.0)
    assert reader.calls == [{"count": 1, "node_id": "main bedroom"}]


def test_missing_reply_is_missing_data_not_an_error():
    reader = FakeReader(None)
    assert controller.read_humidity(reader, "main bed", 1_700_000_000.0) == (None, None)


def test_reply_without_humidity_is_missing_data():
    reader = FakeReader(
        {"latest_humidity_pct": None, "node_id": "main bed", "start_ms": 1_700_000_000_000}
    )
    assert controller.read_humidity(reader, None, 1_700_000_010.0) == (None, None)


def test_future_stamp_is_treated_as_fresh():
    """A board clock behind the server stamp must not mark data permanently stale."""
    reader = FakeReader(
        {
            "latest_humidity_pct": 44,
            "start_ms": 1_700_001_000_000,
            "node_id": "main bed",
        }
    )
    hum, age = controller.read_humidity(reader, "main bedroom", 1_700_000_000.0)
    assert hum == 44
    assert age == 0.0


def test_no_stamp_leaves_the_age_unknown():
    reader = FakeReader({"latest_humidity_pct": 50, "start_ms": 0, "node_id": "main bed"})
    assert controller.read_humidity(reader, "main bedroom", 1_700_000_000.0) == (50, None)


def test_reply_from_another_room_is_refused():
    """A typo'd/unknown node makes the server answer for the NEWEST node; the
    controller must not humidify another room on that data."""
    reader = FakeReader(
        {"latest_humidity_pct": 20, "start_ms": 1_700_000_000_000, "node_id": "study ro"}
    )
    assert controller.read_humidity(reader, "main bedroom", 1_700_000_010.0) == (None, None)


def test_unpinned_read_accepts_whatever_node_replies():
    reader = FakeReader(
        {"latest_humidity_pct": 41, "start_ms": 1_700_000_000_000, "node_id": "study ro"}
    )
    hum, _age = controller.read_humidity(reader, None, 1_700_000_010.0)
    assert hum == 41


def test_on_decision_renews_the_lease():
    sock = FakeSocket()
    controller.apply_decision(sock, Decision(True, "dry"), SP)
    assert sock.calls == [("on", 300)]


def test_off_decision_drops_the_lease():
    sock = FakeSocket()
    controller.apply_decision(sock, Decision(False, "in band"), SP)
    assert sock.calls == [("off", 0)]


def test_socket_failure_propagates():
    with pytest.raises(SocketError):
        controller.apply_decision(FakeSocket(fail=True), Decision(True, "dry"), SP)
