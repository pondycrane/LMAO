"""LMAF codec tests — golden vectors + framing/decoding contracts.

Run with::

    bazel test //tests:test_lma_attachment --test_output=errors

The golden hex vectors below are FROZEN wire facts (generated from the
protobuf stubs, byte-identical to the canonical C++ core
``firmware_common/lma_common/lma_attachment.cpp`` and pinned by its host test
``//firmware_common:lma_attachment_test``).  They are asserted verbatim — the
expected bytes are never recomputed from the code under test.  Where a
``build_transfer`` assertion needs a payload-dependent id/digest, the expected
envelope is built independently through the generated protobuf stubs.
"""

import hashlib
import zlib

import pytest
from proto import lma_messages_pb2 as pb

from lma_core import attachment as lmaf

# ── Golden wire vectors (frozen) ───────────────────────────────────────
GOLDEN_ID = "000102030405060708090a0b0c0d0e0f"
GOLDEN_SHA256 = "a0a1a2a3a4a5a6a7a8a9aaabacadaeafb0b1b2b3b4b5b6b7b8b9babbbcbdbebf"

GOLDEN_MANIFEST = (
    "0a10000102030405060708090a0b0c0d0e0f"
    "1220a0a1a2a3a4a5a6a7a8a9aaabacadaeafb0b1b2b3b4b5b6b7b8b9babbbcbdbebf"
    "180622126c6d616f3a63686172742d6c696e652d7631"
    "28f001300238fc026a10663566303539353233393236323733397080a6c1d59733"
)
GOLDEN_ENVELOPE_MANIFEST = "c2026b" + GOLDEN_MANIFEST  # 107 B envelope

GOLDEN_CHUNK = "0a10000102030405060708090a0b0c0d0e0f10011a03010203251d80bc55"
GOLDEN_ENVELOPE_CHUNK = "ca021e" + GOLDEN_CHUNK  # 32 B envelope

GOLDEN_ACK = "0a10000102030405060708090a0b0c0d0e0f10021801200120032a046c6f7373"
GOLDEN_ENVELOPE_ACK = "d20220" + GOLDEN_ACK  # 34 B envelope

GOLDEN_CAPS = (
    "0801100110061a0b636f646563323a37303043"
    "1a126c6d616f3a63686172742d6c696e652d7631"
    "20f001288080013008"
)
GOLDEN_ENVELOPE_CAPS = "920330" + GOLDEN_CAPS  # 50 B envelope

# The 240-byte opportunistic chunk: data = i % 256 for i in range(240).
PATTERN_240 = bytes(i % 256 for i in range(240))
GOLDEN_ENVELOPE_CHUNK240 = (
    "ca028a020a10000102030405060708090a0b0c0d0e0f1af001"
    + PATTERN_240.hex()
    + "25660b0ba6"
)
GOLDEN_ENVELOPE_CHUNK240_LEN = 270


def _manifest_envelope(**fields) -> bytes:
    """Build a manifest envelope straight from the generated stubs."""
    envelope = pb.LMAOEnvelope()
    envelope.manifest.CopyFrom(pb.AttachmentManifest(**fields))
    return envelope.SerializeToString()


def _chunk_envelope(id_bytes, index, data, crc) -> bytes:
    """Build a chunk envelope straight from the generated stubs."""
    envelope = pb.LMAOEnvelope()
    envelope.chunk.CopyFrom(
        pb.AttachmentChunk(id=id_bytes, index=index, data=data, crc32=crc)
    )
    return envelope.SerializeToString()


class TestCrc32:
    def test_frozen_values(self):
        """CRC-32 must match the frozen vector values (zlib polynomial)."""
        assert lmaf.crc32(b"\x01\x02\x03") == 1438416925
        assert lmaf.crc32(PATTERN_240) == 2785741670

    def test_matches_zlib_crc32(self):
        assert lmaf.crc32(PATTERN_240) == zlib.crc32(PATTERN_240)
        assert lmaf.crc32(b"lmao:chart-line-v1") == zlib.crc32(b"lmao:chart-line-v1")

    def test_seed_continues_a_running_crc(self):
        """A seeded call must continue the CRC, not restart it."""
        assert lmaf.crc32(b"\x03", lmaf.crc32(b"\x01\x02")) == lmaf.crc32(b"\x01\x02\x03")
        assert lmaf.crc32(b"") == 0


class TestGoldenWireVectors:
    """The frozen vectors must be reproduced byte-for-byte by the stubs and
    decoded back to the same field values by the codec."""

    def test_manifest_vector_is_byte_exact(self):
        assert (
            _manifest_envelope(
                id=bytes.fromhex(GOLDEN_ID),
                payload_sha256=bytes.fromhex(GOLDEN_SHA256),
                kind=pb.KIND_CHART,
                codec="lmao:chart-line-v1",
                chunk_size=240,
                chunk_count=2,
                total_bytes=380,
                node_id="f5f0595239262739",
                created_ms=1758700000000,
            ).hex()
            == GOLDEN_ENVELOPE_MANIFEST
        )

    def test_chunk_vector_is_byte_exact(self):
        assert (
            _chunk_envelope(bytes.fromhex(GOLDEN_ID), 1, b"\x01\x02\x03", 1438416925).hex()
            == GOLDEN_ENVELOPE_CHUNK
        )

    def test_ack_vector_is_byte_exact(self):
        envelope = pb.LMAOEnvelope()
        envelope.att_ack.CopyFrom(
            pb.AttachmentAck(
                id=bytes.fromhex(GOLDEN_ID),
                status=pb.AttachmentAck.NEED,
                have_count=1,
                missing=[1, 3],
                reason="loss",
            )
        )
        assert envelope.SerializeToString().hex() == GOLDEN_ENVELOPE_ACK

    def test_caps_vector_is_byte_exact(self):
        envelope = pb.LMAOEnvelope()
        envelope.caps.CopyFrom(
            pb.Capability(
                lmaf_version=1,
                kinds=[pb.KIND_VOICE, pb.KIND_CHART],
                codecs=["codec2:700C", "lmao:chart-line-v1"],
                max_chunk_size=240,
                max_attachment_bytes=16384,
                rx_window=8,
            )
        )
        assert envelope.SerializeToString().hex() == GOLDEN_ENVELOPE_CAPS

    def test_240_byte_chunk_envelope_is_270_bytes(self):
        """The opportunistic chunk profile must stay within one LXMF packet."""
        envelope = _chunk_envelope(
            bytes.fromhex(GOLDEN_ID), 0, PATTERN_240, lmaf.crc32(PATTERN_240)
        )
        assert envelope.hex() == GOLDEN_ENVELOPE_CHUNK240
        assert len(envelope) == GOLDEN_ENVELOPE_CHUNK240_LEN
        assert len(envelope) <= lmaf.MAX_ENVELOPE_BYTES

    def test_decode_manifest_vector(self):
        decoded = lmaf.decode_envelope(bytes.fromhex(GOLDEN_ENVELOPE_MANIFEST))
        assert decoded == {
            "payload": "manifest",
            "id": GOLDEN_ID,
            "payload_sha256": GOLDEN_SHA256,
            "kind": lmaf.KIND_CHART,
            "codec": "lmao:chart-line-v1",
            "chunk_size": 240,
            "chunk_count": 2,
            "total_bytes": 380,
            "sample_rate": 0,
            "channels": 0,
            "duration_ms": 0,
            "width": 0,
            "height": 0,
            "node_id": "f5f0595239262739",
            "created_ms": 1758700000000,
            "meta": {},
        }

    def test_decode_chunk_vector(self):
        decoded = lmaf.decode_envelope(bytes.fromhex(GOLDEN_ENVELOPE_CHUNK))
        assert decoded == {
            "payload": "chunk",
            "id": GOLDEN_ID,
            "index": 1,
            "data": b"\x01\x02\x03",
            "crc32": 1438416925,
        }

    def test_decode_ack_vector(self):
        decoded = lmaf.decode_ack(bytes.fromhex(GOLDEN_ENVELOPE_ACK))
        assert decoded == {
            "payload": "ack",
            "id": GOLDEN_ID,
            "status": lmaf.ACK_NEED,
            "have_count": 1,
            "missing": [1, 3],
            "reason": "loss",
            "retry_after_ms": 0,
        }

    def test_decode_caps_vector(self):
        decoded = lmaf.decode_capability(bytes.fromhex(GOLDEN_ENVELOPE_CAPS))
        assert decoded == {
            "payload": "capability",
            "lmaf_version": 1,
            "kinds": [lmaf.KIND_VOICE, lmaf.KIND_CHART],
            "codecs": ["codec2:700C", "lmao:chart-line-v1"],
            "max_chunk_size": 240,
            "max_attachment_bytes": 16384,
            "rx_window": 8,
            "airtime_budget_bps": 0,
        }

    def test_unknown_fields_are_skipped_like_the_cpp_decoder(self):
        """A newer sender (extra unknown fields) must not break the decoder —
        the C++ test pins the same case (field 99 appended to the vector)."""
        envelope = bytes.fromhex(GOLDEN_ENVELOPE_MANIFEST) + b"\x98\x06\x2a"
        decoded = lmaf.decode_envelope(envelope)
        assert decoded["payload"] == "manifest"
        assert decoded["id"] == GOLDEN_ID
        assert decoded["kind"] == lmaf.KIND_CHART

    def test_decode_240_byte_chunk_round_trips(self):
        decoded = lmaf.decode_envelope(bytes.fromhex(GOLDEN_ENVELOPE_CHUNK240))
        assert decoded["data"] == PATTERN_240
        assert decoded["index"] == 0
        assert decoded["crc32"] == lmaf.crc32(PATTERN_240)
        assert decoded["crc32"] == 2785741670


class TestBuildTransfer:
    def test_manifest_and_chunks_match_independent_stub_encoding(self):
        """build_transfer's framing must equal an independent pb2 encoding."""
        payload = bytes(range(256)) + bytes(range(124))  # 380 B -> 240 + 140
        transfer = lmaf.build_transfer(
            payload,
            kind=lmaf.KIND_CHART,
            codec="lmao:chart-line-v1",
            node_id="f5f0595239262739",
            created_ms=1758700000000,
        )

        digest = hashlib.sha256(payload).digest()
        assert transfer.id == digest[:16].hex()
        assert transfer.payload_sha256 == digest.hex()
        assert transfer.chunk_size == lmaf.CHUNK_SIZE_LO
        assert transfer.chunk_count == 2
        assert transfer.total_bytes == 380

        assert transfer.manifest_envelope == _manifest_envelope(
            id=digest[:16],
            payload_sha256=digest,
            kind=pb.KIND_CHART,
            codec="lmao:chart-line-v1",
            chunk_size=240,
            chunk_count=2,
            total_bytes=380,
            node_id="f5f0595239262739",
            created_ms=1758700000000,
        )
        first, second = payload[:240], payload[240:]
        assert transfer.chunk_envelopes == [
            _chunk_envelope(digest[:16], 0, first, lmaf.crc32(first)),
            _chunk_envelope(digest[:16], 1, second, lmaf.crc32(second)),
        ]

    def test_240_byte_chunk_profile_stays_in_one_packet(self):
        """A full-size chunk's envelope must be 270 B (<= 285 B hard cap)."""
        payload = PATTERN_240
        transfer = lmaf.build_transfer(
            payload,
            kind=lmaf.KIND_IMAGE,
            codec="webp",
            node_id="n0de",
            created_ms=1,
        )
        assert transfer.chunk_count == 1
        assert transfer.chunk_envelopes[0] == _chunk_envelope(
            hashlib.sha256(payload).digest()[:16], 0, payload, lmaf.crc32(payload)
        )
        assert len(transfer.chunk_envelopes[0]) == GOLDEN_ENVELOPE_CHUNK240_LEN
        assert len(transfer.manifest_envelope) <= lmaf.MAX_ENVELOPE_BYTES

    def test_exact_final_chunk(self):
        payload = b"x" * (2 * lmaf.CHUNK_SIZE_LO)
        transfer = lmaf.build_transfer(
            payload, kind=lmaf.KIND_FILE, codec="bin", created_ms=7
        )
        assert transfer.chunk_count == 2
        assert transfer.total_bytes == 480
        assert [len(lmaf.decode_envelope(e)["data"]) for e in transfer.chunk_envelopes] == [
            240,
            240,
        ]

    def test_inexact_final_chunk_count_and_offsets(self):
        """chunk_count is ceil(total/chunk_size) and index*chunk_size is the
        payload offset, so reassembly from the envelopes is byte-exact."""
        payload = bytes(i % 251 for i in range(500))
        transfer = lmaf.build_transfer(
            payload, kind=lmaf.KIND_AUDIO, codec="opus:lbw", chunk_size=240, created_ms=9
        )
        assert transfer.chunk_count == 3
        assert transfer.total_bytes == 500

        rebuilt = bytearray()
        for expected_index, envelope in enumerate(transfer.chunk_envelopes):
            decoded = lmaf.decode_envelope(envelope)
            assert decoded["index"] == expected_index
            assert decoded["id"] == transfer.id
            assert decoded["crc32"] == lmaf.crc32(decoded["data"])
            assert len(decoded["data"]) <= transfer.chunk_size
            assert expected_index * transfer.chunk_size == len(rebuilt)
            rebuilt += decoded["data"]
        assert bytes(rebuilt) == payload
        assert len(transfer.chunk_envelopes[-1]) < len(transfer.chunk_envelopes[0])

    def test_media_descriptors_round_trip(self):
        transfer = lmaf.build_transfer(
            b"\x00\x01",
            kind=lmaf.KIND_VOICE,
            codec="codec2:700C",
            node_id="node-1",
            created_ms=1758700000000,
            sample_rate=8000,
            channels=1,
            duration_ms=1234,
        )
        decoded = lmaf.decode_envelope(transfer.manifest_envelope)
        assert decoded["sample_rate"] == 8000
        assert decoded["channels"] == 1
        assert decoded["duration_ms"] == 1234
        assert decoded["kind"] == lmaf.KIND_VOICE
        assert decoded["node_id"] == "node-1"

    def test_rejects_invalid_chunk_size_and_media_kwargs(self):
        with pytest.raises(ValueError):
            lmaf.build_transfer(b"x", kind=lmaf.KIND_FILE, codec="bin", chunk_size=0)
        with pytest.raises(TypeError):
            lmaf.build_transfer(b"x", kind=lmaf.KIND_IMAGE, codec="webp", pixels=4)


class TestLengthPrefixBoundary:
    """The inner message length crosses 127 -> 128 at ~103 B of chunk data, so
    the envelope's length prefix flips from 1 byte to 2."""

    def test_one_byte_vs_two_byte_length_prefix(self):
        def envelope_for(data):
            transfer = lmaf.build_transfer(
                data, kind=lmaf.KIND_FILE, codec="bin", chunk_size=240, created_ms=1
            )
            return transfer.chunk_envelopes[0]

        one_byte = envelope_for(b"\xaa" * 102)
        two_byte = envelope_for(b"\xaa" * 103)

        # field 41 tag is always the two-byte varint 0xca 0x02; the next byte
        # starts the inner length varint.
        assert one_byte[:2] == b"\xca\x02"
        assert one_byte[2] == 127
        assert two_byte[:2] == b"\xca\x02"
        assert two_byte[2] == 0x80 and two_byte[3] == 0x01

        assert len(one_byte) == 2 + 1 + 127
        assert len(two_byte) == 2 + 2 + 128
        assert len(lmaf.decode_envelope(one_byte)["data"]) == 102
        assert len(lmaf.decode_envelope(two_byte)["data"]) == 103


class TestDecodeNeverRaises:
    def test_empty_input_is_none(self):
        assert lmaf.decode_envelope(b"") is None
        assert lmaf.decode_capability(b"") is None
        assert lmaf.decode_ack(b"") is None

    @pytest.mark.parametrize(
        "garbage",
        [
            b"\xff\xff\xff",
            b"\x0a",  # truncated length-delimited field
            b"\x00",  # field number 0 is illegal
            bytes.fromhex(GOLDEN_ENVELOPE_MANIFEST)[:-1],  # truncated vector
            bytes.fromhex(GOLDEN_ENVELOPE_CAPS)[:5],  # truncated caps
            b"\xca\x02\x05abc",  # chunk field with a length that runs past the end
        ],
    )
    def test_unparsable_input_is_none(self, garbage):
        assert lmaf.decode_envelope(garbage) is None
        assert lmaf.decode_capability(garbage) is None
        assert lmaf.decode_ack(garbage) is None

    @pytest.mark.parametrize(
        "build",
        [
            lambda env: env.text.__setattr__("content", "hello"),
            lambda env: env.sensor.__setattr__("node_id", "sprout"),
            lambda env: env.command.__setattr__("action", "reboot"),
        ],
    )
    def test_non_lmaf_envelope_is_other(self, build):
        envelope = pb.LMAOEnvelope()
        build(envelope)
        assert lmaf.decode_envelope(envelope.SerializeToString()) == {"payload": "other"}
        assert lmaf.decode_capability(envelope.SerializeToString()) is None
        assert lmaf.decode_ack(envelope.SerializeToString()) is None

    def test_decode_helpers_reject_other_lmaf_kinds(self):
        manifest = bytes.fromhex(GOLDEN_ENVELOPE_MANIFEST)
        assert lmaf.decode_capability(manifest) is None
        assert lmaf.decode_ack(manifest) is None
        assert lmaf.decode_ack(bytes.fromhex(GOLDEN_ENVELOPE_CAPS)) is None


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__] + sys.argv[1:]))
