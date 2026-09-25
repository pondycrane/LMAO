"""Server LMAF reply-routing tests (RNS/LXMF mocked, real protobuf stubs).

Run with::

    bazel test //tests:test_server_attachment_reply --test_output=errors

The mock/fake setup follows ``tests/test_server_handler.py`` +
``tests/conftest.py``: ``setup_common_mocks()`` replaces RNS/LXMF/gRPC/config
so ``lmao_server.server`` imports without hardware.  conftest additionally
swaps ``proto.lma_messages_pb2`` for a MagicMock to keep imports hermetic —
these tests assert byte-level LMAF framing, so they re-bind the *real*
generated classes onto ``lma_core`` (conftest itself is untouched; RNS/LXMF
stay mocked).

The RNS delivery callback is sync and runs on the RNS thread; paced LMAF
packets are scheduled onto the server's captured asyncio loop, which these
tests drive manually with ``_drain()`` (pacing is set to 0 so no test sleeps
for the LoRa turnaround).
"""

import asyncio
import hashlib
import logging
import sys
import time
from unittest.mock import MagicMock

import pytest
from conftest import cleanup_common_mocks, setup_common_mocks
from proto import lma_messages_pb2 as pb

from lma_core import attachment as lmaf
from lma_core import sprout_history
from lma_core.sprout_history import SproutHistory

# Sender's lxmf/delivery hash — what the allow-list gate matches on and what
# the capability cache is keyed by.
PEER = "aabbccdd00112233"
SPROUT_NODE = "f5f0595239262739"

_ACK_TEXT = "ACK from LMAO Server — received your message ({n} bytes)"

# The real generated types this test relies on, captured before conftest's
# mocks can shadow them (see the module docstring).
_REAL_PROTO_TYPES = (
    "LMAOEnvelope",
    "TextMessage",
    "SensorReport",
    "SensorReading",
    "CommandRequest",
    "CommandAck",
    "AudioMessage",
    "ImageMessage",
    "CallSignal",
)


# ── envelope builders ────────────────────────────────────────────────


def _caps_envelope(kinds=(pb.KIND_CHART,), version=1, max_chunk_size=240):
    envelope = pb.LMAOEnvelope()
    envelope.caps.CopyFrom(
        pb.Capability(
            lmaf_version=version,
            kinds=list(kinds),
            codecs=["lmao:chart-line-v1"],
            max_chunk_size=max_chunk_size,
            max_attachment_bytes=16384,
            rx_window=8,
        )
    )
    return envelope.SerializeToString()


def _sensor_envelope(seq, moisture):
    envelope = pb.LMAOEnvelope()
    report = envelope.sensor
    report.node_id = SPROUT_NODE
    report.seq = seq
    reading = report.readings.add()
    reading.sensor_id = 4  # soil moisture (%)
    reading.value = moisture
    return envelope.SerializeToString()


def _ack_envelope(transfer_id, status, missing=(), reason=""):
    envelope = pb.LMAOEnvelope()
    envelope.att_ack.CopyFrom(
        pb.AttachmentAck(
            id=bytes.fromhex(transfer_id),
            status=status,
            have_count=0,
            missing=list(missing),
            reason=reason,
        )
    )
    return envelope.SerializeToString()


def _text_envelope(content):
    envelope = pb.LMAOEnvelope()
    envelope.text.node_id = PEER
    envelope.text.content = content
    return envelope.SerializeToString()


def _lxmf_msg(content):
    """Fake inbound LXMF message carrying *content* from :data:`PEER`."""
    message = MagicMock()
    message.get_source.return_value = MagicMock()
    message.get_source.return_value.hash = b"\x02" * 16
    message.content = content
    message.title_as_string.return_value = "p:Envelope"
    return message


def _sent_contents():
    """The content kwarg of every LXMF message the server dispatched."""
    return [
        call.kwargs["content"] for call in sys.modules["LXMF"].LXMessage.call_args_list
    ]


def _reset_sends():
    sys.modules["LXMF"].LXMessage.reset_mock()


def _reply_text(content_bytes):
    envelope = pb.LMAOEnvelope()
    envelope.ParseFromString(content_bytes)
    return envelope.text.content


def _drain(loop, seconds=0.05):
    """Run the captured loop long enough for the paced LMAF sends to finish."""
    loop.run_until_complete(asyncio.sleep(seconds))


def _seed_history(module, samples=80):
    """Give the server a chart line longer than one LMAF chunk."""
    history = SproutHistory(maxlen=samples)
    for i in range(samples):
        report = pb.SensorReport(node_id=SPROUT_NODE, seq=i)
        reading = report.readings.add()
        reading.sensor_id = 4
        reading.value = 30 + (i % 70)
        history.update(report)
    return history


def _seed_air_history(module, samples=25):
    """Air *and* soil samples, so the air-series cap is observable.

    ``SproutHistory`` only records air readings for reports that also carry
    soil moisture, so every seeded report carries all three series.
    """
    history = SproutHistory(maxlen=samples)
    for i in range(samples):
        report = pb.SensorReport(node_id=SPROUT_NODE, seq=i)
        for sensor_id, base in ((3, 20.0), (2, 50.0), (4, 30.0)):
            reading = report.readings.add()
            reading.sensor_id = sensor_id
            reading.value = base + (i % 5)
        history.update(report)
    return history


def _series_counts(line):
    """Per-series sample counts from a ``DATA ...`` line (temp, hum, moisture)."""
    tokens = line.split()
    assert tokens[0] == "DATA", f"not a DATA line: {line[:32]!r}"
    index = 4  # DATA, node8, dry, wet
    counts = []
    for _ in range(3):
        count = int(tokens[index])
        counts.append(count)
        index += 1 + count
    assert index == len(tokens), f"trailing tokens in DATA line: {line!r}"
    return counts


def _lmaf_payload(sent):
    """Reassemble the LMAF transfer in *sent* back to (manifest, payload).

    The manifest is sent more than once (a transfer cannot survive losing it),
    so skip any leading manifests before reassembling the chunks.
    """
    manifest = lmaf.decode_envelope(sent[0])
    chunks = [lmaf.decode_envelope(c) for c in sent if lmaf.decode_envelope(c)["payload"] == "chunk"]
    assert [c["index"] for c in chunks] == list(range(manifest["chunk_count"]))
    return manifest, b"".join(c["data"] for c in chunks)


# ── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def lmaf_server(monkeypatch):
    """A Server with mocked RNS/LXMF and real protobuf types."""
    for mod in ("lmao_server.server", "lmao_server", "server"):
        sys.modules.pop(mod, None)
    setup_common_mocks(with_grpc=True)

    # conftest's setup_common_mocks() swaps proto.lma_messages_pb2 for a
    # MagicMock so lma_core imports hermetically; these tests assert real wire
    # bytes, so restore the generated stubs before anything (re)imports the
    # codec, and re-bind the real types over the mock attributes (which
    # otherwise shadow lma_core's lazy __getattr__).
    sys.modules.pop("proto", None)
    sys.modules.pop("proto.lma_messages_pb2", None)
    import proto.lma_messages_pb2 as real_pb2

    import lma_core

    for name in _REAL_PROTO_TYPES:
        setattr(lma_core, name, getattr(real_pb2, name))

    from lmao_server import server as server_module

    server_module.ALLOWED_CLIENTS = server_module.ALLOWED_CLIENTS | {PEER}
    sys.modules["RNS"].hexrep.return_value = PEER
    server_module.LMAF_CAPS.clear()
    server_module.SPROUT_HISTORY.reset()
    # LoRa turnaround pacing would otherwise make every test sleep.
    monkeypatch.setattr(server_module, "LMAO_LMAF_PACING_SECONDS", 0.0)
    monkeypatch.setattr(server_module, "LMAO_LMAF_TURNAROUND_SECONDS", 0.0)

    loop = asyncio.new_event_loop()
    instance = server_module.Server()
    instance.router = MagicMock()
    instance.server_identity = MagicMock()
    instance.server_identity.hash = b"\x01" * 16
    instance._loop = loop

    # LMAF packets are sent fire-and-forget (LXMessage.pack() + a raw
    # RNS.Packet, no receipt) so LXMF's proof-waiting retry loop cannot churn
    # the LoRa path.  Make the mocked LXMessage behave like the real one so that
    # path is exercised instead of the link fallback.
    lxmf = sys.modules["LXMF"]
    lxmf.LXMessage.return_value.packed = b"\x00" * 16 + b"LMAF-PACKED"
    lxmf.LXMessage.return_value.method = lxmf.LXMessage.OPPORTUNISTIC

    yield instance, server_module, loop

    loop.close()
    cleanup_common_mocks()


# ── tests ────────────────────────────────────────────────────────────


class TestLmafPeerReply:
    def test_chart_goes_out_as_manifest_and_chunks_without_data_line(
        self, lmaf_server, monkeypatch, caplog
    ):
        instance, module, loop = lmaf_server
        monkeypatch.setattr(module, "SPROUT_HISTORY", _seed_history(module))

        # Peer announces it can receive charts before the report.
        instance.handle_lxmf_delivery(_lxmf_msg(_caps_envelope()))
        assert module.LMAF_CAPS.chart_capable(PEER)

        _reset_sends()
        content = _sensor_envelope(seq=1, moisture=44)
        with caplog.at_level(logging.INFO):
            instance.handle_lxmf_delivery(_lxmf_msg(content))
        _drain(loop)

        expected_line = module.SPROUT_HISTORY.data_line()
        assert len(expected_line) > lmaf.CHUNK_SIZE_LO

        sent = _sent_contents()
        # Manifests first (repeated — see LMAF_MANIFEST_REPEATS), then chunks.
        assert len(sent) >= 3

        # 1) No ACK text at all for an LMAF peer: it acknowledges the transfer
        #    itself, and a text reply would be transmitted into the window where
        #    the peer cannot hear (half duplex).
        for raw in sent:
            parsed = lmaf.decode_envelope(raw)
            assert parsed["payload"] != "other", "no text reply for an LMAF peer"

        # 2) Manifest first, for exactly the current chart line.
        manifest = lmaf.decode_envelope(sent[0])
        assert manifest["payload"] == "manifest"
        assert manifest["kind"] == lmaf.KIND_CHART
        assert manifest["codec"] == "lmao:chart-line-v1"
        assert manifest["node_id"] == PEER
        assert manifest["chunk_size"] == lmaf.CHUNK_SIZE_LO
        assert manifest["total_bytes"] == len(expected_line)
        assert manifest["chunk_count"] >= 2
        assert manifest["payload_sha256"] == hashlib.sha256(
            expected_line.encode()
        ).hexdigest()
        assert manifest["id"] == manifest["payload_sha256"][:32]

        # 3) Chunks follow in index order and reassemble to the exact line.
        chunks = [
            lmaf.decode_envelope(c)
            for c in sent
            if lmaf.decode_envelope(c)["payload"] == "chunk"
        ]
        assert len(chunks) == manifest["chunk_count"]
        assert [c["index"] for c in chunks] == list(range(manifest["chunk_count"]))
        assert all(c["id"] == manifest["id"] for c in chunks)
        assert all(c["crc32"] == lmaf.crc32(c["data"]) for c in chunks)
        assert b"".join(c["data"] for c in chunks) == expected_line.encode()

        # 4) Pending state is tracked for NEED resends and logged.
        assert manifest["id"] in instance._lmaf_pending
        assert (
            f"LMAF send id={manifest['id'][:8]} kind=6 "
            f"chunks={manifest['chunk_count']} bytes={len(expected_line)}"
        ) in caplog.text

    def test_lmaf_peer_gets_the_full_air_series(self, lmaf_server, monkeypatch):
        """The point of sending a chart over LMAF: the air series is no longer
        capped at DATA_AIR_MAX_SAMPLES, while a legacy peer keeps the capped
        line (the single-packet budget it piggybacks on)."""
        instance, module, loop = lmaf_server
        monkeypatch.setattr(module, "SPROUT_HISTORY", _seed_air_history(module, samples=25))
        air_samples = len(module.SPROUT_HISTORY.temp)
        assert air_samples > sprout_history.DATA_AIR_MAX_SAMPLES

        # A peer that never advertised LMAF: capped, single reply.
        _reset_sends()
        instance.handle_lxmf_delivery(_lxmf_msg(_sensor_envelope(seq=1, moisture=44)))
        _drain(loop)
        sent = _sent_contents()
        assert len(sent) == 1
        legacy_line = _reply_text(sent[0]).split("\n", 1)[1]
        assert _series_counts(legacy_line) == [
            sprout_history.DATA_AIR_MAX_SAMPLES,
            sprout_history.DATA_AIR_MAX_SAMPLES,
            25,
        ]

        # The same history to an LMAF-capable peer: full rings, chunked.
        instance.handle_lxmf_delivery(_lxmf_msg(_caps_envelope()))
        _reset_sends()
        instance.handle_lxmf_delivery(_lxmf_msg(_sensor_envelope(seq=2, moisture=45)))
        _drain(loop)
        sent = _sent_contents()
        manifest, payload = _lmaf_payload(sent)
        # Uncapped: every seeded sample is on the wire.  The DATA-line cap
        # would have truncated the air series to DATA_AIR_MAX_SAMPLES.
        assert _series_counts(payload.decode()) == [air_samples] * 3
        assert payload == module.SPROUT_HISTORY.data_line(air_limit=None).encode()
        assert manifest["total_bytes"] == len(payload)
        assert manifest["chunk_count"] == (len(payload) + lmaf.CHUNK_SIZE_LO - 1) // lmaf.CHUNK_SIZE_LO
        assert manifest["chunk_count"] >= 2, "full air rings must need more than one chunk"

    def test_lmaf_packets_are_sent_fire_and_forget(self, lmaf_server, monkeypatch):
        """LMAF packets must not go through LXMF's router: reference LXMF waits
        for a Reticulum proof per packet, and a peer that does not prove makes it
        retry, churn the path and finally drop the message — which is fatal to a
        multi-packet transfer and wasteful on a duty-cycled medium."""
        instance, module, loop = lmaf_server
        monkeypatch.setattr(module, "SPROUT_HISTORY", _seed_history(module))
        _reset_sends()
        module.RNS.Packet.reset_mock()
        instance.router.handle_outbound.reset_mock()

        instance.handle_lxmf_delivery(_lxmf_msg(_caps_envelope()))
        instance.handle_lxmf_delivery(_lxmf_msg(_sensor_envelope(seq=1, moisture=44)))
        _drain(loop)

        manifest = lmaf.decode_envelope(_sent_contents()[0])
        packets = module.RNS.Packet.call_args_list
        # One packet per LMAF message: the manifest is repeated, and each chunk
        # follows.  No text reply for an LMAF peer, so the router is not used.
        assert len(packets) == module.LMAF_MANIFEST_REPEATS + manifest["chunk_count"]
        assert packets[0].args[1] == b"LMAF-PACKED", "packed payload without dest16"
        assert instance.router.handle_outbound.call_count == 0, (
            "LMAF transfers must not use the router's proof-waiting path"
        )

    def test_legacy_peer_still_gets_the_data_line_on_the_ack(
        self, lmaf_server, monkeypatch
    ):
        """A peer that never advertised LMAF keeps today's reply shape —
        even for a chart line that exceeds the LMAF chunk budget."""
        instance, module, loop = lmaf_server
        monkeypatch.setattr(module, "SPROUT_HISTORY", _seed_history(module))

        _reset_sends()
        content = _sensor_envelope(seq=5, moisture=51)
        instance.handle_lxmf_delivery(_lxmf_msg(content))
        _drain(loop)

        expected_line = module.SPROUT_HISTORY.data_line()
        assert len(expected_line) > lmaf.CHUNK_SIZE_LO

        sent = _sent_contents()
        assert len(sent) == 1, "legacy peer gets one reply, never LMAF packets"
        assert _reply_text(sent[0]) == _ACK_TEXT.format(n=len(content)) + "\n" + expected_line
        assert not instance._lmaf_pending
        assert lmaf.decode_envelope(sent[0]) == {"payload": "other"}


class TestCapabilityCache:
    def test_caps_envelope_populates_the_cache(self, lmaf_server):
        instance, module, _loop = lmaf_server

        instance.handle_lxmf_delivery(
            _lxmf_msg(_caps_envelope(kinds=(pb.KIND_VOICE, pb.KIND_CHART)))
        )

        entry = module.LMAF_CAPS.get(PEER)
        assert entry is not None
        assert entry["lmaf_version"] == 1
        assert entry["kinds"] == [lmaf.KIND_VOICE, lmaf.KIND_CHART]
        assert entry["max_chunk_size"] == 240
        assert isinstance(entry["updated"], float)
        assert module.LMAF_CAPS.chart_capable(PEER)
        # Control traffic is answered with nothing at all.
        assert not _sent_contents()

    @pytest.mark.parametrize(
        "caps",
        [
            _caps_envelope(kinds=(pb.KIND_VOICE,)),  # no chart support
            _caps_envelope(version=0),  # pre-LMAF capability
        ],
        ids=["no-chart-kind", "version-zero"],
    )
    def test_peer_without_chart_capability_keeps_legacy_reply(
        self, lmaf_server, monkeypatch, caps
    ):
        instance, module, loop = lmaf_server
        monkeypatch.setattr(module, "SPROUT_HISTORY", _seed_history(module))
        instance.handle_lxmf_delivery(_lxmf_msg(caps))
        _reset_sends()

        content = _sensor_envelope(seq=2, moisture=60)
        instance.handle_lxmf_delivery(_lxmf_msg(content))
        _drain(loop)

        sent = _sent_contents()
        assert len(sent) == 1
        assert _reply_text(sent[0]).endswith("\n" + module.SPROUT_HISTORY.data_line())

    def test_cache_is_keyed_by_sender_hash(self, lmaf_server):
        """A caps envelope from one peer must not enable LMAF for another."""
        instance, module, _loop = lmaf_server
        instance.handle_lxmf_delivery(_lxmf_msg(_caps_envelope()))
        assert module.LMAF_CAPS.get("0011223344556677") is None
        assert module.LMAF_CAPS.chart_capable("0011223344556677") is False


def _send_chart(instance, module, loop):
    """Drive one report through the LMAF path; returns (transfer_id, chunk_count)."""
    instance.handle_lxmf_delivery(_lxmf_msg(_caps_envelope()))
    _reset_sends()
    instance.handle_lxmf_delivery(_lxmf_msg(_sensor_envelope(seq=1, moisture=44)))
    _drain(loop)
    manifest = lmaf.decode_envelope(_sent_contents()[0])
    _reset_sends()
    return manifest["id"], manifest["chunk_count"]


class TestAckHandling:
    def test_need_ack_resends_listed_chunks_up_to_three_attempts(
        self, lmaf_server, monkeypatch, caplog
    ):
        instance, module, loop = lmaf_server
        monkeypatch.setattr(module, "SPROUT_HISTORY", _seed_history(module))
        transfer_id, chunk_count = _send_chart(instance, module, loop)
        assert chunk_count >= 2

        for attempt in (1, 2, 3):
            with caplog.at_level(logging.INFO):
                instance.handle_lxmf_delivery(
                    _lxmf_msg(
                        _ack_envelope(
                            transfer_id, pb.AttachmentAck.NEED, missing=[0, 1]
                        )
                    )
                )
            _drain(loop)
            resent = [lmaf.decode_envelope(c) for c in _sent_contents()]
            assert [c["index"] for c in resent] == [0, 1], f"attempt {attempt}"
            assert all(c["id"] == transfer_id for c in resent)
            assert f"LMAF resend id={transfer_id[:8]} chunks=2 attempt={attempt}" in caplog.text
            _reset_sends()

        # Fourth NEED: attempt budget exhausted — give up, no more airtime.
        instance.handle_lxmf_delivery(
            _lxmf_msg(_ack_envelope(transfer_id, pb.AttachmentAck.NEED, missing=[0]))
        )
        _drain(loop)
        assert _sent_contents() == []
        assert (
            f"LMAF giving up id={transfer_id[:8]} after 3 resend attempts" in caplog.text
        )
        assert transfer_id not in instance._lmaf_pending

    def test_need_ack_for_unknown_transfer_is_ignored(self, lmaf_server, caplog):
        instance, _module, loop = lmaf_server
        unknown = "ab" * 16
        with caplog.at_level(logging.INFO):
            instance.handle_lxmf_delivery(
                _lxmf_msg(_ack_envelope(unknown, pb.AttachmentAck.NEED, missing=[0]))
            )
        _drain(loop)
        assert _sent_contents() == []
        assert f"LMAF resend asked for unknown transfer id={unknown[:8]}" in caplog.text

    def test_empty_need_is_a_manifest_request(self, lmaf_server, monkeypatch, caplog):
        """A peer that holds chunks but never got the manifest asks with an
        empty NEED; the only recovery is to resend the whole transfer."""
        instance, module, loop = lmaf_server
        monkeypatch.setattr(module, "SPROUT_HISTORY", _seed_history(module))
        transfer_id, chunk_count = _send_chart(instance, module, loop)
        assert chunk_count >= 2

        with caplog.at_level(logging.INFO):
            instance.handle_lxmf_delivery(
                _lxmf_msg(
                    _ack_envelope(
                        transfer_id, pb.AttachmentAck.NEED, missing=[], reason="manifest"
                    )
                )
            )
        _drain(loop)

        resent = [lmaf.decode_envelope(c) for c in _sent_contents()]
        assert resent[0]["payload"] == "manifest", "the manifest must come first"
        assert resent[0]["id"] == transfer_id
        assert [c["index"] for c in resent[1:]] == list(range(chunk_count))
        assert (
            f"LMAF manifest request id={transfer_id[:8]} chunks={chunk_count} "
            "— resending manifest + chunks"
        ) in caplog.text

        # It is a resend like any other: the per-transfer budget is shared.
        for _ in range(3):
            instance.handle_lxmf_delivery(
                _lxmf_msg(_ack_envelope(transfer_id, pb.AttachmentAck.NEED, missing=[]))
            )
            _drain(loop)
        assert transfer_id not in instance._lmaf_pending, "attempt budget must apply"

    def test_complete_ack_logs_once_and_drops_pending_state(
        self, lmaf_server, monkeypatch, caplog
    ):
        instance, module, loop = lmaf_server
        monkeypatch.setattr(module, "SPROUT_HISTORY", _seed_history(module))
        transfer_id, chunk_count = _send_chart(instance, module, loop)
        expected_bytes = len(module.SPROUT_HISTORY.data_line())

        with caplog.at_level(logging.INFO):
            instance.handle_lxmf_delivery(
                _lxmf_msg(_ack_envelope(transfer_id, pb.AttachmentAck.COMPLETE))
            )
        assert (
            f"LMAF complete id={transfer_id[:8]} bytes={expected_bytes} "
            f"chunks={chunk_count}"
        ) in caplog.text
        assert transfer_id not in instance._lmaf_pending
        assert _sent_contents() == []

    def test_rejected_and_aborted_acks_log_and_drop_state(
        self, lmaf_server, monkeypatch, caplog
    ):
        instance, module, loop = lmaf_server
        monkeypatch.setattr(module, "SPROUT_HISTORY", _seed_history(module))
        for status, label in (
            (pb.AttachmentAck.REJECTED, "rejected"),
            (pb.AttachmentAck.ABORTED, "aborted"),
        ):
            module.LMAF_CAPS.clear()
            instance._lmaf_pending.clear()
            transfer_id, _chunks = _send_chart(instance, module, loop)
            caplog.clear()
            with caplog.at_level(logging.INFO):
                instance.handle_lxmf_delivery(
                    _lxmf_msg(
                        _ack_envelope(transfer_id, status, reason="too large")
                    )
                )
            assert (
                f"LMAF {label} id={transfer_id[:8]} reason=too large" in caplog.text
            )
            assert transfer_id not in instance._lmaf_pending

    def test_pending_transfers_stay_bounded(self, lmaf_server, monkeypatch):
        instance, module, loop = lmaf_server
        monkeypatch.setattr(module, "SPROUT_HISTORY", _seed_history(module))
        instance.handle_lxmf_delivery(_lxmf_msg(_caps_envelope()))
        _reset_sends()

        for seq in range(module.LMAF_MAX_PENDING + 3):
            instance.handle_lxmf_delivery(
                _lxmf_msg(_sensor_envelope(seq=seq, moisture=40 + seq))
            )
            _drain(loop)

        assert len(instance._lmaf_pending) == module.LMAF_MAX_PENDING
        # The oldest transfers were evicted, the newest kept.
        decoded = [lmaf.decode_envelope(c) for c in _sent_contents()]
        manifests = [d for d in decoded if d["payload"] == "manifest"]
        # Each manifest goes out LMAF_MANIFEST_REPEATS times, so dedupe
        # (order-preserving) before comparing with the pending set.
        newest_ids = list(dict.fromkeys(m["id"] for m in manifests))[
            -module.LMAF_MAX_PENDING :
        ]
        assert list(instance._lmaf_pending.keys()) == newest_ids


class TestLmafPathHandling:
    """The transport discards packets when a path entry disappears mid-send.

    LXMF's receipt-waiting retry loop used to hide that for the legacy reply;
    LMAF packets are sent once, so the sender must wait for the path (which
    costs no airtime — nothing is transmitted while it is missing) and report
    the drop when it still cannot send.
    """

    def test_transfer_waits_for_a_missing_path_instead_of_dropping(
        self, lmaf_server, monkeypatch, caplog
    ):
        instance, module, loop = lmaf_server
        monkeypatch.setattr(module, "SPROUT_HISTORY", _seed_history(module))
        monkeypatch.setattr(module, "LMAO_LMAF_PATH_WAIT_SECONDS", 0.0)

        rns = sys.modules["RNS"]
        state = {"known": False, "requests": 0}

        def fake_request(_hash):
            state["requests"] += 1
            state["known"] = True  # the peer's announce restored the path

        monkeypatch.setattr(rns.Transport, "has_path", lambda _hash: state["known"])
        monkeypatch.setattr(rns.Transport, "request_path", fake_request)

        instance.handle_lxmf_delivery(_lxmf_msg(_caps_envelope()))
        _reset_sends()
        with caplog.at_level(logging.INFO):
            instance.handle_lxmf_delivery(
                _lxmf_msg(_sensor_envelope(seq=1, moisture=44))
            )
            _drain(loop)

        assert state["requests"] == 1, "a missing path must be requested"
        sent = _sent_contents()
        assert sent, "the transfer must go out once the path is back"
        assert lmaf.decode_envelope(sent[0])["payload"] == "manifest"
        assert f"path to {PEER[:8]} restored by a path request" in caplog.text

    def test_dropped_packet_is_reported(self, lmaf_server, monkeypatch, caplog):
        instance, module, loop = lmaf_server
        monkeypatch.setattr(module, "SPROUT_HISTORY", _seed_history(module))
        rns = sys.modules["RNS"]
        # RNS.Packet.send() returns False (not None) when the transport drops
        # the packet because no interface could process it.
        monkeypatch.setattr(rns.Packet.return_value, "send", lambda: False)

        instance.handle_lxmf_delivery(_lxmf_msg(_caps_envelope()))
        _reset_sends()
        with caplog.at_level(logging.WARNING):
            instance.handle_lxmf_delivery(
                _lxmf_msg(_sensor_envelope(seq=1, moisture=44))
            )
            _drain(loop)

        assert f"transport dropped the packet for {PEER[:8]}" in caplog.text

    def test_exhausted_path_wait_is_reported(self, lmaf_server, monkeypatch, caplog):
        instance, module, loop = lmaf_server
        monkeypatch.setattr(module, "SPROUT_HISTORY", _seed_history(module))
        monkeypatch.setattr(module, "LMAO_LMAF_PATH_WAIT_SECONDS", 0.0)
        rns = sys.modules["RNS"]
        monkeypatch.setattr(rns.Transport, "has_path", lambda _hash: False)
        monkeypatch.setattr(rns.Transport, "request_path", lambda _hash: None)

        instance.handle_lxmf_delivery(_lxmf_msg(_caps_envelope()))
        _reset_sends()
        with caplog.at_level(logging.WARNING):
            instance.handle_lxmf_delivery(
                _lxmf_msg(_sensor_envelope(seq=1, moisture=44))
            )
            _drain(loop)

        assert (
            f"no path to {PEER[:8]} after {module.LMAF_PATH_WAIT_ATTEMPTS} "
            "path requests"
        ) in caplog.text


class TestLmafDeadLetter:
    """A transfer the peer never noticed at all must not vanish.

    Receiver-driven recovery (NEED for missing chunks, an empty NEED for a lost
    manifest) needs the peer to have *seen* something.  A transfer whose every
    frame was lost produces no ack of any kind, so the sender re-offers the
    manifest — one packet, and the primitive that recovers any partial state —
    on the shared backoff schedule, bounded, then gives up loudly instead of
    dropping the payload in silence.
    """

    def test_unacked_transfer_is_reoffered_then_abandoned(
        self, lmaf_server, monkeypatch, caplog
    ):
        instance, module, loop = lmaf_server
        monkeypatch.setattr(module, "SPROUT_HISTORY", _seed_history(module))
        transfer_id, _chunks = _send_chart(instance, module, loop)
        sent_at = instance._lmaf_pending[transfer_id]["sent_at"]

        # Not due: the shared policy waits 30 s before the first re-offer.
        assert instance._lmaf_retry_pass(now=sent_at + 29.0) == 0
        assert transfer_id in instance._lmaf_pending

        with caplog.at_level(logging.INFO):
            _reset_sends()
            assert instance._lmaf_retry_pass(now=sent_at + 31.0) == 1
            _drain(loop)
            reoffer = _sent_contents()
            assert len(reoffer) == 1, "a re-offer costs one packet, not a transfer"
            decoded = lmaf.decode_envelope(reoffer[0])
            assert decoded["payload"] == "manifest"
            assert decoded["id"] == transfer_id
            assert f"LMAF re-offer id={transfer_id[:8]} attempt=1/3" in caplog.text

            # Backoff: 30 s, then 90 s, then 270 s, then the budget is spent.
            assert instance._lmaf_retry_pass(now=sent_at + 31.0 + 89.0) == 0
            assert instance._lmaf_retry_pass(now=sent_at + 31.0 + 91.0) == 1
            assert instance._lmaf_retry_pass(now=sent_at + 122.0 + 269.0) == 0
            assert instance._lmaf_retry_pass(now=sent_at + 122.0 + 271.0) == 1
            assert instance._lmaf_retry_pass(now=sent_at + 1_000_000.0) == 0
            _drain(loop)

        assert f"LMAF abandoned id={transfer_id[:8]} after 3 re-offers" in caplog.text
        assert transfer_id not in instance._lmaf_pending
        assert len(_sent_contents()) == 3, "three re-offers, then silence"

    def test_acked_transfer_is_never_reoffered(self, lmaf_server, monkeypatch):
        instance, module, loop = lmaf_server
        monkeypatch.setattr(module, "SPROUT_HISTORY", _seed_history(module))
        transfer_id, _chunks = _send_chart(instance, module, loop)

        instance.handle_lxmf_delivery(
            _lxmf_msg(_ack_envelope(transfer_id, pb.AttachmentAck.COMPLETE))
        )
        _drain(loop)
        assert transfer_id not in instance._lmaf_pending

        _reset_sends()
        assert instance._lmaf_retry_pass(now=1e18) == 0
        _drain(loop)
        assert _sent_contents() == [], "a completed transfer is never re-offered"

    def test_need_ack_resets_the_dead_letter_clock(self, lmaf_server, monkeypatch):
        instance, module, loop = lmaf_server
        monkeypatch.setattr(module, "SPROUT_HISTORY", _seed_history(module))
        transfer_id, _chunks = _send_chart(instance, module, loop)

        # Backdate the send so its first deadline is already history.
        entry = instance._lmaf_pending[transfer_id]
        entry["sent_at"] = time.monotonic() - 10_000.0
        stale = entry["sent_at"]

        # A peer that is alive and asking for parts restarts the clock, so the
        # stale deadline no longer fires a re-offer.
        instance.handle_lxmf_delivery(
            _lxmf_msg(_ack_envelope(transfer_id, pb.AttachmentAck.NEED, missing=[0]))
        )
        _drain(loop)
        assert entry["sent_at"] > stale, "any ack restarts the dead-letter clock"

        _reset_sends()
        assert instance._lmaf_retry_pass(now=stale + 60.0) == 0
        assert transfer_id in instance._lmaf_pending, "a live peer is not abandoned"


def test_retry_policy_matches_the_cpp_core():
    """Parity pin with firmware_common/lma_common/lma_attachment.h::RetryPolicy.

    Its host test asserts the same literals: the server and any future
    device-side sender re-offer on one schedule, not two.
    """
    assert [lmaf.retry_delay_seconds(i) for i in range(3)] == [30.0, 90.0, 270.0]
    assert lmaf.RETRY_MAX_ATTEMPTS == 3


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__] + sys.argv[1:]))


class TestDeadLetterDuplicates:
    """A dead-letter re-send must prove delivery without counting twice.

    The client re-transmits the *identical* envelope when no reply arrives, so
    the server skips the ingestion side effects of a repeat (chart buffer,
    ingest pipeline) but still answers it — that answer is the delivery proof
    the client is waiting for.
    """

    def test_identical_resend_is_answered_but_not_reingested(
        self, lmaf_server, monkeypatch, caplog
    ):
        instance, module, loop = lmaf_server
        # Small ring: a saturated one hides whether a sample landed at all.
        monkeypatch.setattr(module, "SPROUT_HISTORY", SproutHistory(maxlen=8))
        _reset_sends()
        content = _sensor_envelope(seq=7, moisture=41)

        instance.handle_lxmf_delivery(_lxmf_msg(content))
        _drain(loop)
        assert len(module.SPROUT_HISTORY.samples) == 1
        assert _sent_contents(), "the first report is answered"

        _reset_sends()
        with caplog.at_level(logging.INFO):
            instance.handle_lxmf_delivery(_lxmf_msg(content))  # byte-identical re-send
            _drain(loop)

        assert len(module.SPROUT_HISTORY.samples) == 1, (
            "a re-send must not be counted as new telemetry"
        )
        assert _sent_contents(), "a re-send is still answered (that answer is the proof)"
        assert "byte-identical re-send" in caplog.text

    def test_new_readings_from_the_same_peer_are_still_ingested(
        self, lmaf_server, monkeypatch
    ):
        """The guard keys on bytes, not on the peer: fresh telemetry differs."""
        instance, module, loop = lmaf_server
        monkeypatch.setattr(module, "SPROUT_HISTORY", SproutHistory(maxlen=8))

        instance.handle_lxmf_delivery(_lxmf_msg(_sensor_envelope(seq=8, moisture=42)))
        _drain(loop)
        instance.handle_lxmf_delivery(_lxmf_msg(_sensor_envelope(seq=9, moisture=43)))
        _drain(loop)

        assert len(module.SPROUT_HISTORY.samples) == 2, "new readings still land"
        assert module.SPROUT_HISTORY.samples[-1] == 43.0, "and the newest value is the one kept"

    def test_repeated_capability_is_not_a_duplicate(self, lmaf_server, caplog):
        """Control traffic is idempotent by nature, not a re-send.

        The device re-advertises its capabilities every 120 s until the server
        proves it understood (an inbound manifest/chunk), so that byte-identical
        repeat is normal protocol traffic.  Observed live: the guard flagged it
        as a duplicate, which reads as a fault and is simply wrong.
        """
        instance, _module, loop = lmaf_server
        caps = _caps_envelope()   # deterministic bytes, as on the wire
        with caplog.at_level(logging.INFO):
            instance.handle_lxmf_delivery(_lxmf_msg(caps))
            instance.handle_lxmf_delivery(_lxmf_msg(caps))
            _drain(loop)

        assert caplog.text.count("LMAF caps from") == 2, (
            "both advertisements must be processed"
        )
        assert "byte-identical re-send" not in caplog.text, (
            "a capability repeat is not a duplicate delivery"
        )
