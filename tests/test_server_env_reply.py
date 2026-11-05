"""Server reply routing for air temperature/humidity requests.

Exercises the REAL ``lmao_server.server`` module (no conftest RNS/LXMF mocks)
so the protobuf envelopes and the ``EnvHistory``/``SproutHistory`` buffers are
the production objects.  Only the LXMF transport (``LXMF.LXMessage``) and the
destination wrapping are replaced, since no Reticulum instance runs here.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

from proto import lma_messages_pb2 as pb

from lma_core.env_history import EnvHistory
from lmao_server import server

AIR_NODE = "cardputer01"
REQUESTER_HASH = "983c04117aea3d4ca8f48962355624fc"
AIR_TEMP = 21.5
AIR_HUM = 55.0


class _FakeLXMessage:
    OPPORTUNISTIC = 1

    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _monotonic_clock(start=1_700_000_000_000, step=60_000):
    """Deterministic server-receive clock: each report advances the stamp."""
    state = {"t": start}

    def tick():
        value = state["t"]
        state["t"] += step
        return value

    return tick


def _source(hash_hex):
    """A fake LXMF source destination (only .hash is read)."""
    source = MagicMock()
    source.hash = bytes.fromhex(hash_hex)
    return source


def _sensor_report(node_id=AIR_NODE, temp=AIR_TEMP, humidity=AIR_HUM,
                   ts=1_700_000_000_000):
    report = pb.SensorReport()
    report.node_id = node_id
    report.seq = 1
    if temp is not None:
        reading = report.readings.add()
        reading.sensor_id = 3  # ambient temperature
        reading.value = temp
        reading.unit = pb.UNIT_CELSIUS
        reading.timestamp_ms = ts
    if humidity is not None:
        reading = report.readings.add()
        reading.sensor_id = 2  # ambient humidity
        reading.value = humidity
        reading.unit = pb.UNIT_PERCENT
        reading.timestamp_ms = ts
    return report


def _air_request(count=0, since_ms=0, series=(pb.Series.AIR_TEMP, pb.Series.AIR_HUMIDITY)):
    envelope = pb.LMAOEnvelope()
    envelope.seq = 9
    envelope.request.history.count = count
    envelope.request.history.since_ms = since_ms
    envelope.request.history.series.extend(series)
    return envelope


@pytest.fixture
def server_instance():
    # A deterministic clock: samples are ordered by server receive time, and
    # two reports inside the same millisecond would tie.
    server.ENV_HISTORY = EnvHistory(clock=_monotonic_clock())
    server.SPROUT_HISTORY.reset()
    server.REPORT_NAMES.clear()
    server.ANNOUNCED_NAMES.clear()
    instance = server.Server()
    instance.router = MagicMock()
    instance.server_identity = MagicMock()
    instance.server_identity.hexhash = "serverhexhash"
    with (
        patch.object(server, "LXMF") as fake_lxmf,
        patch.object(server, "_identity_to_destination", return_value="SRC-DEST"),
    ):
        fake_lxmf.LXMessage = _FakeLXMessage
        yield instance, fake_lxmf
    server.ENV_HISTORY.reset()
    server.SPROUT_HISTORY.reset()
    server.REPORT_NAMES.clear()
    server.ANNOUNCED_NAMES.clear()


def _replies(fake_lxmf, router):
    """Parse every LXMF reply the router was handed into an envelope."""
    out = []
    for call in router.handle_outbound.call_args_list:
        message = call.args[0]
        assert message.kwargs["title"] == "p:Envelope"
        assert message.kwargs["destination"] is not None
        envelope = pb.LMAOEnvelope()
        envelope.ParseFromString(message.kwargs["content"])
        out.append(envelope)
    return out


class TestAirOnlyHistoryRequest:
    def test_air_only_request_is_answered_from_the_env_buffer(
        self, server_instance
    ):
        instance, fake_lxmf = server_instance
        server.ENV_HISTORY.update(_sensor_report())

        instance._send_lxmf_reply(MagicMock(), _air_request(count=1))

        replies = _replies(fake_lxmf, instance.router)
        assert len(replies) == 1
        assert replies[0].WhichOneof("payload") == "chart"
        chart = replies[0].chart
        assert list(chart.temp) == [215], "wire unit is tenths of °C"
        assert list(chart.hum) == [55]
        assert len(chart.soil) == 0
        assert chart.node_id == AIR_NODE[:8]
        assert chart.start_ms == 1_700_000_000_000
        assert chart.period_ms == 5 * 60 * 1000

    def test_air_only_request_without_samples_draws_no_reply(
        self, server_instance
    ):
        instance, fake_lxmf = server_instance
        instance._send_lxmf_reply(MagicMock(), _air_request())
        assert _replies(fake_lxmf, instance.router) == []

    def test_since_watermark_beyond_the_samples_draws_no_reply(
        self, server_instance
    ):
        instance, fake_lxmf = server_instance
        server.ENV_HISTORY.update(_sensor_report())
        instance._send_lxmf_reply(
            MagicMock(), _air_request(since_ms=1_700_000_100_000)
        )
        assert _replies(fake_lxmf, instance.router) == []

    def test_sprout_soil_chart_is_unaffected(self, server_instance):
        """A soil-series request still answers from SproutHistory."""
        instance, fake_lxmf = server_instance
        sprout = pb.SensorReport()
        sprout.node_id = "e824ad2da00d2c8a45c2db6e700c989b"
        reading = sprout.readings.add()
        reading.sensor_id = 4  # soil moisture
        reading.value = 46.0
        server.SPROUT_HISTORY.update(sprout)
        server.ENV_HISTORY.update(sprout)

        instance._send_lxmf_reply(
            MagicMock(), _air_request(series=(pb.Series.SOIL,))
        )

        replies = _replies(fake_lxmf, instance.router)
        assert len(replies) == 1
        chart = replies[0].chart
        assert list(chart.soil) == [46]
        assert list(chart.temp) == []
        assert list(chart.hum) == []

    def test_mixed_request_falls_back_to_the_env_buffer(self, server_instance):
        """SOIL+AIR with no soil history answers with the air series."""
        instance, fake_lxmf = server_instance
        server.ENV_HISTORY.update(_sensor_report())

        instance._send_lxmf_reply(
            MagicMock(),
            _air_request(series=(pb.Series.SOIL, pb.Series.AIR_TEMP)),
        )

        replies = _replies(fake_lxmf, instance.router)
        assert len(replies) == 1
        chart = replies[0].chart
        assert list(chart.temp) == [215]
        assert len(chart.soil) == 0

    def test_request_ack_still_replies(self, server_instance):
        instance, fake_lxmf = server_instance
        envelope = pb.LMAOEnvelope()
        envelope.seq = 4
        envelope.request_ack = True
        instance._send_lxmf_reply(MagicMock(), envelope)

        replies = _replies(fake_lxmf, instance.router)
        assert len(replies) == 1
        assert replies[0].WhichOneof("payload") == "ack"
        assert replies[0].ack.seq == 4

    def test_reply_is_scoped_to_the_requesters_own_node(self, server_instance):
        """Each device charts ITS OWN history, not the newest reporter's.

        The scope key is what the device REPORTS (SensorReport.node_id), which
        is how EnvHistory keys its rings — not the contact-book display name.
        """
        instance, fake_lxmf = server_instance
        server.ENV_HISTORY.update(
            _sensor_report(node_id="main bedroom", temp=20.0, humidity=40.0)
        )
        server.ENV_HISTORY.update(
            _sensor_report(node_id="kitchen", temp=25.0, humidity=60.0)
        )
        server.REPORT_NAMES[REQUESTER_HASH] = "main bedroom"
        try:
            instance._send_lxmf_reply(_source(REQUESTER_HASH), _air_request(count=1))
        finally:
            server.REPORT_NAMES.clear()

        replies = _replies(fake_lxmf, instance.router)
        assert len(replies) == 1
        chart = replies[0].chart
        assert chart.node_id == "main bed"
        assert list(chart.temp) == [200]
        assert list(chart.hum) == [40]

    def test_operator_rename_does_not_move_the_scope(self, server_instance):
        """Renaming a contact must not make its board chart ANOTHER node's
        series: scoping stays on the reported node_id, not the display name."""
        instance, fake_lxmf = server_instance
        server.ENV_HISTORY.update(
            _sensor_report(node_id="main bedroom", temp=20.0, humidity=40.0)
        )
        server.ENV_HISTORY.update(
            _sensor_report(node_id="kitchen", temp=25.0, humidity=60.0)
        )
        server.REPORT_NAMES[REQUESTER_HASH] = "main bedroom"
        book = MagicMock()
        book.find.return_value = [{"device_name": "Kitchen"}]
        try:
            with patch.object(server, "CONTACTS", book):
                instance._send_lxmf_reply(
                    _source(REQUESTER_HASH), _air_request(count=1)
                )
        finally:
            server.REPORT_NAMES.clear()

        chart = _replies(fake_lxmf, instance.router)[0].chart
        assert chart.node_id == "main bed"
        assert list(chart.temp) == [200]

    def test_unknown_requester_gets_the_newest_node(self, server_instance):
        instance, fake_lxmf = server_instance
        server.ENV_HISTORY.update(
            _sensor_report(node_id="main bedroom", temp=20.0, humidity=40.0,
                          ts=1_700_000_000_000)
        )
        server.ENV_HISTORY.update(
            _sensor_report(node_id="kitchen", temp=25.0, humidity=60.0,
                          ts=1_700_000_300_000)
        )
        with patch.object(server, "_requester_node_name", return_value=None):
            instance._send_lxmf_reply(MagicMock(), _air_request(count=1))

        chart = _replies(fake_lxmf, instance.router)[0].chart
        assert chart.node_id == "kitchen"[:8]


class TestAnnounceNames:
    def test_announce_name_is_recorded(self):
        import RNS.vendor.umsgpack as umsgpack

        server.ANNOUNCED_NAMES.clear()
        handler = server._AnnounceNameHandler()
        handler.received_announce(
            bytes.fromhex(REQUESTER_HASH),
            MagicMock(),
            umsgpack.packb([b"main bedroom", None, [1]]),
        )
        assert server.ANNOUNCED_NAMES[REQUESTER_HASH] == "main bedroom"
        server.ANNOUNCED_NAMES.clear()

    def test_malformed_announce_is_ignored(self):
        server.ANNOUNCED_NAMES.clear()
        handler = server._AnnounceNameHandler()
        handler.received_announce(b"\x01" * 16, MagicMock(), b"not-msgpack")
        assert server.ANNOUNCED_NAMES == {}

    def test_reported_node_id_wins_over_the_announce_and_the_book(self):
        """Scoping uses the node_id the device reports — the ring's own key."""
        server.REPORT_NAMES[REQUESTER_HASH] = "reported"
        server.ANNOUNCED_NAMES[REQUESTER_HASH] = "announced"
        book = MagicMock()
        book.find.return_value = [{"device_name": "operator-set"}]
        try:
            with patch.object(server, "CONTACTS", book):
                assert server._requester_node_name(_source(REQUESTER_HASH)) == "reported"
        finally:
            server.REPORT_NAMES.clear()
            server.ANNOUNCED_NAMES.clear()

    def test_announce_names_a_device_that_has_not_reported_yet(self):
        server.REPORT_NAMES.clear()
        server.ANNOUNCED_NAMES[REQUESTER_HASH] = "announced"
        try:
            assert server._requester_node_name(_source(REQUESTER_HASH)) == "announced"
        finally:
            server.ANNOUNCED_NAMES.clear()

    def test_contact_book_is_the_last_resort(self):
        server.REPORT_NAMES.clear()
        server.ANNOUNCED_NAMES.clear()
        book = MagicMock()
        book.find.return_value = [{"device_name": "operator-set"}]
        with patch.object(server, "CONTACTS", book):
            assert server._requester_node_name(_source(REQUESTER_HASH)) == "operator-set"


class TestSensorFolding:
    def test_sensor_report_reaches_the_env_buffer(self, server_instance):
        instance, _fake_lxmf = server_instance
        server.ALLOWED_CLIENTS = server.ALLOWED_CLIENTS | {"testhash1234"}
        envelope = pb.LMAOEnvelope()
        envelope.sensor.CopyFrom(_sensor_report())

        message = MagicMock()
        message.content = envelope.SerializeToString()
        message.title_as_string.return_value = "p:Envelope"

        with patch.object(server.RNS, "hexrep", return_value="testhash1234"):
            instance.handle_lxmf_delivery(message)

        latest = server.ENV_HISTORY.latest()
        assert latest is not None
        assert latest["node_id"] == AIR_NODE
        assert latest["temp_c"] == AIR_TEMP
        assert latest["humidity_pct"] == AIR_HUM
        # A plain sensor report draws no reply (reporting is one-way).
        assert instance.router.handle_outbound.call_count == 0

    def test_unauthorized_sender_does_not_fold(self, server_instance):
        instance, _fake_lxmf = server_instance
        envelope = pb.LMAOEnvelope()
        envelope.sensor.CopyFrom(_sensor_report())

        message = MagicMock()
        message.content = envelope.SerializeToString()
        message.title_as_string.return_value = "p:Envelope"

        with patch.object(server.RNS, "hexrep", return_value="deadbeef"):
            instance.handle_lxmf_delivery(message)

        assert server.ENV_HISTORY.latest() is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
