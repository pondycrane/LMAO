"""Controller glue tests: room extraction, decision application, state file.

The Reticulum client is faked here (``request_env`` returns the same summary
dict ``summarize_chart`` produces), so these exercise the controller's own
logic without a mesh.
"""

import json
import os

import pytest

from lmao_socket_humidifier import controller
from lmao_socket_humidifier.control import ControlState, Decision, Setpoints
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
    def __init__(self, fail=False, status=None):
        self.calls = []
        self.fail = fail
        self.status = status or {}

    def set_on(self, lease_s):
        self.calls.append(("on", lease_s))
        if self.fail:
            raise SocketError("boom")
        return {"relay": True, "lease_s": lease_s, **self.status}

    def set_off(self):
        self.calls.append(("off", 0))
        if self.fail:
            raise SocketError("boom")
        return {"relay": False, "lease_s": 0, **self.status}


def _summary(**kw):
    base = {
        "latest_temp_c": 24.2,
        "latest_humidity_pct": 37,
        "start_ms": 1_700_000_000_000,
        "node_id": "main bed",
    }
    base.update(kw)
    return base


def test_reading_pins_the_env_node_and_derives_sample_age():
    reader = FakeReader(_summary())
    temp, hum, age = controller.read_room(reader, "main bedroom", 1_700_000_012.0)
    assert (temp, hum) == (24.2, 37)
    assert age == pytest.approx(12.0)
    assert reader.calls == [{"count": 1, "node_id": "main bedroom"}]


def test_missing_reply_is_missing_data_not_an_error():
    reader = FakeReader(None)
    assert controller.read_room(reader, "main bed", 1_700_000_000.0) == (None, None, None)


def test_reply_without_humidity_is_missing_data():
    reader = FakeReader(_summary(latest_humidity_pct=None))
    assert controller.read_room(reader, None, 1_700_000_000.0) == (None, None, None)


def test_reply_without_temperature_is_missing_data():
    """Dew point needs both halves of the reading."""
    reader = FakeReader(_summary(latest_temp_c=None))
    assert controller.read_room(reader, None, 1_700_000_000.0) == (None, None, None)


def test_reply_for_another_node_is_refused():
    """A typo'd --env-node must not silently drive the actuator from the
    server's newest-reporter fallback."""
    reader = FakeReader(_summary(node_id="kitchen"))
    assert controller.read_room(reader, "main bedroom", 1_700_000_000.0) == (None, None, None)


def test_future_stamp_is_treated_as_fresh():
    reader = FakeReader(_summary(start_ms=1_700_000_600_000))
    _temp, _hum, age = controller.read_room(reader, None, 1_700_000_000.0)
    assert age == 0.0


def test_on_decision_renews_the_lease():
    sock = FakeSocket()
    controller.apply_decision(sock, Decision(True, "dry"), SP)
    assert sock.calls == [("on", 300)]


def test_off_decision_drops_the_lease():
    sock = FakeSocket()
    controller.apply_decision(sock, Decision(False, "in band"), SP)
    assert sock.calls == [("off", 0)]


def test_off_is_skipped_when_the_relay_is_already_open():
    """No lease, relay open: re-POSTing /off every cycle is a no-op."""
    sock = FakeSocket()
    status = {"relay": False, "lease_s": 0, "power_w": 0.64}
    assert controller.apply_decision(sock, Decision(False, "in band"), SP, status) is status
    assert sock.calls == []


def test_off_is_still_sent_while_a_lease_is_running_out():
    # relay False but a lease still armed: the socket may close it again, so
    # the explicit OFF is the safe action.
    sock = FakeSocket()
    status = {"relay": False, "lease_s": 42}
    controller.apply_decision(sock, Decision(False, "in band"), SP, status)
    assert sock.calls == [("off", 0)]


def test_on_always_renews_even_when_the_relay_is_closed():
    # The ON request *is* the deadman renewal; skipping it would drop the
    # relay mid-run.
    sock = FakeSocket()
    status = {"relay": True, "lease_s": 120}
    controller.apply_decision(sock, Decision(True, "humidifying"), SP, status)
    assert sock.calls == [("on", 300)]


def test_apply_decision_returns_the_socket_status():
    sock = FakeSocket(status={"power_w": 17.0})
    status = controller.apply_decision(sock, Decision(True, "x"), SP)
    assert status["relay"] is True and status["power_w"] == 17.0


def test_socket_failure_propagates():
    with pytest.raises(SocketError):
        controller.apply_decision(FakeSocket(fail=True), Decision(True, "dry"), SP)


def test_load_warning_flags_standby_power():
    warn = controller.load_warning({"meter_fresh": True, "power_w": 0.62})
    assert warn and "out of water" in warn


def test_load_warning_is_silent_for_a_running_appliance():
    assert controller.load_warning({"meter_fresh": True, "power_w": 17.0}) is None
    # An open relay never decodes the meter, so there is nothing to judge.
    assert controller.load_warning({"meter_fresh": False, "power_w": 0.62}) is None
    assert controller.load_warning(None) is None


def test_state_file_round_trips(tmp_path):
    path = str(tmp_path / "nested" / "state.json")
    st = ControlState(relay_on=True, on_since_s=10.0, on_accum_s=120.0, day_index=7)
    controller.save_state(path, st)
    assert controller.load_state(path) == st
    assert not [p for p in os.listdir(os.path.dirname(path)) if p.startswith(".state-")]


def test_missing_state_file_starts_fresh(tmp_path):
    assert controller.load_state(str(tmp_path / "absent.json")) == ControlState()


def test_corrupt_state_file_starts_fresh(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    assert controller.load_state(str(path)) == ControlState()


def test_empty_state_path_disables_persistence(tmp_path):
    controller.save_state("", ControlState(relay_on=True))
    assert controller.load_state("") == ControlState()


def test_state_file_is_valid_json(tmp_path):
    path = tmp_path / "state.json"
    controller.save_state(str(path), ControlState(relay_on=True, on_accum_s=42.0))
    assert json.loads(path.read_text())["on_accum_s"] == 42.0


def test_socket_reading_is_the_direct_room_input():
    """The socket's ENV III is the room's sensor: no server round trip."""
    status = {"sensor_ok": True, "temp_c": 21.4, "hum_pct": 44, "sensor_age_s": 12}
    assert controller.read_socket_room(status) == (21.4, 44, 12.0)


def test_socket_without_a_sensor_is_missing_data_not_zero():
    """A socket with no ENV III must not read as 0 C / 0 %RH (the law would
    humidify against a fabricated bone-dry room)."""
    assert controller.read_socket_room({"sensor_ok": False}) == (None, None, None)
    assert controller.read_socket_room({"sensor_ok": True, "sensor_age_s": -1}) == (
        None,
        None,
        None,
    )
    assert controller.read_socket_room(None) == (None, None, None)


def test_socket_reading_falls_back_to_the_server_when_stale():
    """auto: a stale socket sample must not shadow a fresh server reading."""
    args = controller._parse_args(["--once", "--sensor-source", "auto"])
    assert args.sensor_source == "auto"
    assert args.stale_s == 900
    # The selection rule the loop implements: socket is used only while fresh.
    sp = Setpoints(stale_s=900)
    status = {"sensor_ok": True, "temp_c": 21.4, "hum_pct": 44, "sensor_age_s": 1200}
    _t, _h, age = controller.read_socket_room(status)
    assert age > sp.stale_s
