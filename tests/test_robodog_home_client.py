"""Host tests for the RoboDog home client (``robodog_home_client/``).

No Reticulum hardware, no network: the wire helpers run against the real
protobuf stubs, the config runs against ``lma_core.config_utils``, and the
request/reply round trip runs against a fake RNS/LXMF pair injected into the
client module (same dependency-injection style as ``lma_core.rns_di``).
"""

import sys
from unittest.mock import patch

import pytest

from robodog_home_client import client as home_client
from robodog_home_client import config as home_config

from lma_core import LMAOEnvelope, Series

CHART_TEMP = [214, 226]
CHART_HUM = [50, 55]
SERVER_HASH = b"\x02" * 16


def _chart_envelope(temp=CHART_TEMP, hum=CHART_HUM):
    envelope = LMAOEnvelope()
    bundle = envelope.chart
    bundle.format = 1
    bundle.node_id = "cardpute"
    bundle.start_ms = 1_700_000_000_000
    bundle.period_ms = 300_000
    bundle.temp.extend(temp)
    bundle.hum.extend(hum)
    return envelope.SerializeToString()


class TestWireHelpers:
    def test_build_history_request_selects_only_the_air_series(self):
        envelope = LMAOEnvelope()
        envelope.ParseFromString(home_client.build_history_request(seq=7, count=5, since_ms=123))
        assert envelope.seq == 7
        assert envelope.request.HasField("history")
        history = envelope.request.history
        assert history.count == 5
        assert history.since_ms == 123
        assert list(history.series) == [Series.AIR_TEMP, Series.AIR_HUMIDITY]
        assert envelope.WhichOneof("payload") is None

    def test_parse_chart_reply_returns_the_bundle(self):
        chart = home_client.parse_chart_reply(_chart_envelope())
        assert chart is not None
        assert list(chart.temp) == CHART_TEMP
        assert list(chart.hum) == CHART_HUM
        assert chart.node_id == "cardpute"

    def test_parse_chart_reply_ignores_other_payloads(self):
        ack = LMAOEnvelope()
        ack.ack.seq = 1
        ack.ack.success = True
        assert home_client.parse_chart_reply(ack.SerializeToString()) is None
        assert home_client.parse_chart_reply(b"not a protobuf") is None
        assert home_client.parse_chart_reply(b"") is None

    def test_summarize_chart_converts_tenths_to_celsius(self):
        summary = home_client.summarize_chart(
            home_client.parse_chart_reply(_chart_envelope())
        )
        assert summary["temps_c"] == [21.4, 22.6]
        assert summary["humidity_pct"] == [50, 55]
        assert summary["latest_temp_c"] == 22.6
        assert summary["latest_humidity_pct"] == 55
        assert summary["node_id"] == "cardpute"
        assert summary["start_ms"] == 1_700_000_000_000
        assert summary["period_ms"] == 300_000

    def test_summarize_empty_series_reports_none(self):
        summary = home_client.summarize_chart(
            home_client.parse_chart_reply(_chart_envelope(temp=[], hum=[42]))
        )
        assert summary["latest_temp_c"] is None
        assert summary["latest_humidity_pct"] == 42

    def test_format_summary_prints_celsius_and_fahrenheit(self):
        summary = home_client.summarize_chart(
            home_client.parse_chart_reply(_chart_envelope(temp=[200], hum=[45]))
        )
        text = home_client.format_summary(summary)
        assert "node cardpute" in text
        assert "20.0 °C (68.0 °F)" in text
        assert "45 %" in text


class TestConfig:
    def test_tcp_client_role_is_tcp_only(self):
        cfg = home_config.make_config("tp4", 4246)
        interfaces = cfg.get_config_dict()["interfaces"]
        assert set(interfaces) == {"Home network TCP"}
        tcp = interfaces["Home network TCP"]
        assert tcp["type"] == "TCPClientInterface"
        assert tcp["target_host"] == "tp4"
        assert tcp["target_port"] == 4246
        assert tcp["enabled"] is True

    def test_config_content_has_no_rnode_or_autointerface(self):
        content = home_config.get_config_content("192.168.0.44", 4246)
        assert "[[Home network TCP]]" in content
        assert "TCPClientInterface" in content
        assert "target_host = 192.168.0.44" in content
        assert "RNodeInterface" not in content
        assert "AutoInterface" not in content

    def test_env_overrides_are_honoured(self):
        with patch.dict(
            "os.environ", {"LMAO_SERVER_HOST": "envhost", "LMAO_SERVER_PORT": "5555"}
        ):
            cfg = home_config.make_config()
        tcp = cfg.get_config_dict()["interfaces"]["Home network TCP"]
        assert tcp["target_host"] == "envhost"
        assert tcp["target_port"] == 5555

    def test_get_configdir_writes_the_config_file(self):
        import shutil
        from pathlib import Path

        path = home_config.get_configdir("tp4", 4246)
        try:
            content = (Path(path) / "config").read_text()
        finally:
            shutil.rmtree(path, ignore_errors=True)
        assert "[[Home network TCP]]" in content
        assert "target_host = tp4" in content
        assert "target_port = 4246" in content


class TestAnnounceMatching:
    @staticmethod
    def _handler(**kwargs):
        return home_client._ServerAnnounceHandler(**kwargs)

    def test_pinned_hash_match_accepts(self):
        handler = self._handler(dest_hash_hex=SERVER_HASH.hex())
        handler.received_announce(SERVER_HASH, object(), None)
        assert handler.event.is_set()
        assert handler.destination_hash == SERVER_HASH

    def test_pinned_hash_mismatch_is_ignored(self):
        handler = self._handler(dest_hash_hex=(b"\x03" * 16).hex())
        handler.received_announce(SERVER_HASH, object(), None)
        assert not handler.event.is_set()
        assert handler.destination_hash is None

    def test_own_delivery_hash_is_ignored(self):
        handler = self._handler(
            dest_hash_hex=SERVER_HASH.hex(), own_hash_hex=SERVER_HASH.hex()
        )
        handler.received_announce(SERVER_HASH, object(), None)
        assert not handler.event.is_set()

    def test_display_name_match_without_a_pin(self):
        import RNS.vendor.umsgpack as umsgpack

        app_data = umsgpack.packb([b"lmao-server", None, [1]])
        handler = self._handler()
        handler.received_announce(SERVER_HASH, object(), app_data)
        assert handler.event.is_set()

    def test_other_display_name_is_ignored(self):
        import RNS.vendor.umsgpack as umsgpack

        app_data = umsgpack.packb([b"robodog-home", None, [1]])
        handler = self._handler()
        handler.received_announce(SERVER_HASH, object(), app_data)
        assert not handler.event.is_set()


# ── Fake RNS/LXMF for the request/reply round trip ──────────────────────


class _FakeDestination:
    def __init__(self, identity, direction, dtype, app_name, *aspects):
        self.identity = identity
        self.direction = direction
        self.app_name = app_name
        self.aspects = aspects
        # Distinct hashes per direction so the own-announce skip is testable.
        self.hash = b"\x01" * 16 if identity == "client" else SERVER_HASH


class _FakeRNS:
    class Destination:
        OUT = 1
        IN = 2
        SINGLE = 3

        def __init__(self, identity, direction, dtype, app_name, *aspects):
            self._inner = _FakeDestination(identity, direction, dtype, app_name, *aspects)
            self.hash = self._inner.hash
            self.identity = identity

    class Transport:
        handlers = []
        path_requests = []
        # One connected interface, so wait_for_interface() succeeds.
        interfaces = [type("Iface", (), {"online": True})()]

        @classmethod
        def register_announce_handler(cls, handler):
            cls.handlers.append(handler)

        @classmethod
        def deregister_announce_handler(cls, handler):
            if handler in cls.handlers:
                cls.handlers.remove(handler)

        @classmethod
        def request_path(cls, dest_hash):
            cls.path_requests.append(dest_hash)

    @staticmethod
    def hexrep(value, delimit=False):
        return value.hex()


class _FakeLXMF:
    class LXMessage:
        OPPORTUNISTIC = 1

        def __init__(self, **kwargs):
            self.kwargs = kwargs


class _FakeRouter:
    def __init__(self, on_outbound=None):
        self.delivery_destinations = {b"\x01" * 16: object()}
        self.announced = []
        self.sent = []
        self._on_outbound = on_outbound

    def announce(self, dest_hash):
        self.announced.append(dest_hash)

    def handle_outbound(self, message):
        self.sent.append(message)
        if self._on_outbound is not None:
            self._on_outbound(message)


class _Reply:
    def __init__(self, content):
        self.content = content


@pytest.fixture
def fake_stack(monkeypatch):
    """Inject fake RNS/LXMF + a fake RNS bootstrap into the client module."""
    router = _FakeRouter()
    monkeypatch.setattr(home_client, "RNS", _FakeRNS)
    monkeypatch.setattr(home_client, "LXMF", _FakeLXMF)
    monkeypatch.setattr(_FakeRNS.Transport, "handlers", [])
    monkeypatch.setattr(_FakeRNS.Transport, "path_requests", [])
    monkeypatch.setattr(
        _FakeRNS.Transport,
        "interfaces",
        [type("Iface", (), {"online": True})()],
    )
    monkeypatch.setattr(
        home_client,
        "init_rns_and_lxmf",
        lambda **kwargs: ("client", router),
    )
    return router


class TestRequestEnv:
    def test_request_env_round_trip(self, fake_stack, tmp_path):
        router = fake_stack
        client = home_client.HomeClient(
            server_host="tp4",
            server_port=4246,
            dest_hash=SERVER_HASH.hex(),
            identity_dir=str(tmp_path),
            timeout=1.0,
            configdir_factory=lambda: str(tmp_path),
        )
        client.start()

        # The server's announce arrives (pinned hash), so discovery resolves.
        client._announce.received_announce(SERVER_HASH, "server-identity", None)

        # The fake router answers the request synchronously with a ChartBundle.
        def answer(message):
            client.handle_lxmf_delivery(_Reply(_chart_envelope()))

        router._on_outbound = answer

        summary = client.request_env(count=2, since_ms=100)
        assert summary is not None
        assert summary["latest_temp_c"] == 22.6
        assert summary["latest_humidity_pct"] == 55

        # Outbound was one air-only history request addressed to the server.
        assert len(router.sent) == 1
        sent = router.sent[0]
        envelope = LMAOEnvelope()
        envelope.ParseFromString(sent.kwargs["content"])
        assert sent.kwargs["title"] == "p:Envelope"
        assert sent.kwargs["desired_method"] == _FakeLXMF.LXMessage.OPPORTUNISTIC
        assert sent.kwargs["destination"].hash == SERVER_HASH
        assert list(envelope.request.history.series) == [
            Series.AIR_TEMP,
            Series.AIR_HUMIDITY,
        ]
        assert envelope.request.history.count == 2
        assert envelope.request.history.since_ms == 100

        # Pinned hash triggered a path request; the handler was registered.
        assert _FakeRNS.Transport.path_requests == [SERVER_HASH]
        assert client._announce in _FakeRNS.Transport.handlers

        client.stop()
        assert client._announce is None
        assert _FakeRNS.Transport.handlers == []

    def test_request_env_times_out_without_a_server(self, fake_stack, tmp_path):
        client = home_client.HomeClient(
            dest_hash=SERVER_HASH.hex(),
            identity_dir=str(tmp_path),
            timeout=0.05,
            configdir_factory=lambda: str(tmp_path),
        )
        client.start()
        assert client.request_env() is None
        assert fake_stack.sent == []
        client.stop()

    def test_request_waits_for_a_connected_interface(self, fake_stack, tmp_path):
        """A packet handed to Transport before the TCP interface connects is
        dropped silently, so the request must not be sent until it is online."""
        client = home_client.HomeClient(
            dest_hash=SERVER_HASH.hex(),
            identity_dir=str(tmp_path),
            timeout=0.05,
            configdir_factory=lambda: str(tmp_path),
        )
        client.start()
        client._announce.received_announce(SERVER_HASH, "server-identity", None)
        with patch.object(home_client, "RNS", _FakeRNS):
            _FakeRNS.Transport.interfaces = []
            assert client.wait_for_interface(0.2) is False
            assert client.request_env() is None
            assert fake_stack.sent == []
            _FakeRNS.Transport.interfaces = [type("Iface", (), {"online": True})()]
            assert client.wait_for_interface(0.2) is True
        client.stop()

    def test_handle_lxmf_delivery_ignores_non_chart_replies(self, fake_stack, tmp_path):
        client = home_client.HomeClient(
            identity_dir=str(tmp_path), configdir_factory=lambda: str(tmp_path)
        )
        ack = LMAOEnvelope()
        ack.ack.success = True
        client.handle_lxmf_delivery(_Reply(ack.SerializeToString()))
        assert client._chart is None
        assert not client._chart_event.is_set()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
