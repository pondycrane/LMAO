"""LMAF — LMAO Attachment Framing (server-side codec).

Transport-agnostic split/reassemble framing for payloads that do not fit one
LXMF packet (chart histories, voice notes, photos, audio, video).  Wire-
compatible with ``proto/lma_messages.proto`` fields 40 (manifest), 41 (chunk),
42 (att_ack) and 50 (caps); read that file's LMAF section before changing
anything here.

The canonical framing implementation is the C++ core
``firmware_common/lma_common/lma_attachment.{h,cpp}`` (host test
``//firmware_common:lma_attachment_test``).  This module is the Python half of
that contract: the generated protobuf stubs are the oracle, and the C++ core
emits byte-identical output.  Invariants that matter here:

* A LMAF envelope MUST ride in ONE opportunistic LXMF packet.  With the
  ``"p:Envelope"`` title and the msgpack frame that leaves <= 285 B for the
  LMAOEnvelope, and a chunk envelope costs 30 B + ``data``; hence the 240 B
  opportunistic-transport chunk profile (:data:`CHUNK_SIZE_LO`), which measures
  a 270 B envelope.  Raising it makes LXMF silently fall back to link delivery,
  which a half-duplex LoRa leaf node cannot service.
* ``id`` = ``sha256(payload)[:16]`` (content address / session key);
  ``payload_sha256`` = the full 32-byte digest.
* A chunk's offset is ``index * chunk_size`` and its ``crc32`` is CRC-32 (zlib
  polynomial) over ``data``; 0 means "not computed".

Mirrors the C++ decoder's never-throw contract: :func:`decode_envelope` returns
``None`` for empty/unparsable input and ``{"payload": "other"}`` for a valid
LMAOEnvelope that carries no LMAF field.
"""

import hashlib
import logging
import time
import zlib
from dataclasses import dataclass

from proto import lma_messages_pb2 as _pb2

_logger = logging.getLogger(__name__)

# ── Transport profile ──────────────────────────────────────────────────
# Opportunistic LXMF chunk data size (see the module docstring): the largest
# chunk whose envelope still fits one packet on a LoRa leaf node.
CHUNK_SIZE_LO = 240

# Hard cap the C++ host test asserts for any LMAF envelope.
MAX_ENVELOPE_BYTES = 285

# ── Sender-side retry ("dead letter") policy ───────────────────────────
# Mirrors RetryPolicy in firmware_common/lma_common/lma_attachment.h — the
# single source of truth.  Normal LMAF recovery is receiver-driven (a NEED names
# the missing chunks, an empty NEED asks for the manifest), but a transfer the
# peer never noticed at all produces no ack of any kind, so the sender must
# re-offer the manifest itself — bounded, because on a 1% duty-cycle band an
# unbounded retry is indistinguishable from a jammer.  Both test suites assert
# this same literal sequence so the senders cannot drift apart.
RETRY_MAX_ATTEMPTS = 3
RETRY_FIRST_DELAY_SECONDS = 30.0
RETRY_BACKOFF_FACTOR = 3.0


def retry_delay_seconds(attempt: int) -> float:
    """Seconds to wait before re-offer *attempt* (0-based): 30, 90, 270."""
    return RETRY_FIRST_DELAY_SECONDS * (RETRY_BACKOFF_FACTOR ** attempt)

# ── AttachmentKind (proto enum values, kept as plain ints so this module
#    never depends on enum wrappers) ────────────────────────────────────
KIND_VOICE = 1
KIND_AUDIO = 2
KIND_IMAGE = 3
KIND_VIDEO = 4
KIND_FILE = 5
KIND_CHART = 6
KIND_SIGNAL = 7

# ── AttachmentAck.Status ───────────────────────────────────────────────
ACK_RECEIVING = 0
ACK_COMPLETE = 1
ACK_NEED = 2
ACK_REJECTED = 3
ACK_ABORTED = 4

# Manifest media descriptors accepted as ``**media`` by :func:`build_transfer`.
_MEDIA_FIELDS = ("sample_rate", "channels", "duration_ms", "width", "height")


@dataclass
class Transfer:
    """One LMAF session: a manifest envelope plus its chunk envelopes."""

    id: str  # hex, 16 bytes (sha256(payload)[:16])
    payload_sha256: str  # hex, 32 bytes (full digest)
    chunk_size: int
    chunk_count: int
    total_bytes: int
    manifest_envelope: bytes  # LMAOEnvelope carrying manifest (field 40)
    chunk_envelopes: list[bytes]  # LMAOEnvelope carrying chunk (field 41)


def crc32(data: bytes, seed: int = 0) -> int:
    """CRC-32 (reflected, zlib/uzlib polynomial) over *data*.

    Identical to ``zlib.crc32(data, seed)`` and to the C++ core's
    ``lma_attachment::crc32`` — the seed lets a caller continue a running CRC.
    """
    return zlib.crc32(data, seed) & 0xFFFFFFFF


def _wrap(field_name: str, message) -> bytes:
    """Serialize *message* as the LMAOEnvelope whose oneof field it fills."""
    envelope = _pb2.LMAOEnvelope()
    getattr(envelope, field_name).CopyFrom(message)
    return envelope.SerializeToString()


def build_transfer(
    payload: bytes,
    *,
    kind: int,
    codec: str,
    node_id: str = "",
    chunk_size: int = CHUNK_SIZE_LO,
    created_ms: int | None = None,
    **media,
) -> Transfer:
    """Frame *payload* as an LMAF manifest + chunk envelopes.

    ``chunk_size`` defaults to the opportunistic-transport profile
    (:data:`CHUNK_SIZE_LO`); pass a larger one only for a transport that can
    carry it (the receiver advertises its own limit via ``Capability``).
    ``created_ms`` defaults to now.  ``**media`` maps to the manifest's media
    descriptors: ``sample_rate``, ``channels``, ``duration_ms``, ``width``,
    ``height``.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be >= 1")
    unknown = sorted(set(media) - set(_MEDIA_FIELDS))
    if unknown:
        raise TypeError(f"unsupported media field(s): {', '.join(unknown)}")

    data = bytes(payload)
    digest = hashlib.sha256(data).digest()
    id_bytes = digest[:16]
    total_bytes = len(data)
    # ceil-division: the final chunk may be short (or the payload empty).
    chunk_count = (total_bytes + chunk_size - 1) // chunk_size if total_bytes else 0
    if created_ms is None:
        created_ms = int(time.time() * 1000)

    manifest = _pb2.AttachmentManifest(
        id=id_bytes,
        payload_sha256=digest,
        kind=kind,
        codec=codec,
        chunk_size=chunk_size,
        chunk_count=chunk_count,
        total_bytes=total_bytes,
        node_id=node_id,
        created_ms=created_ms,
        **media,
    )

    chunk_envelopes = []
    for index in range(chunk_count):
        chunk_data = data[index * chunk_size : (index + 1) * chunk_size]
        chunk = _pb2.AttachmentChunk(
            id=id_bytes,
            index=index,
            data=chunk_data,
            crc32=crc32(chunk_data),
        )
        chunk_envelopes.append(_wrap("chunk", chunk))

    return Transfer(
        id=id_bytes.hex(),
        payload_sha256=digest.hex(),
        chunk_size=chunk_size,
        chunk_count=chunk_count,
        total_bytes=total_bytes,
        manifest_envelope=_wrap("manifest", manifest),
        chunk_envelopes=chunk_envelopes,
    )


def decode_envelope(envelope_bytes: bytes) -> dict | None:
    """Decode one LMAOEnvelope, never raising.

    Returns ``{"payload": "manifest"|"chunk"|"ack"|"capability", ...fields...}``
    for a LMAF envelope, ``{"payload": "other"}`` for a valid LMAOEnvelope that
    carries no LMAF field (sensor/text/command/...), and ``None`` for empty or
    unparsable input.
    """
    if not envelope_bytes:
        return None
    try:
        envelope = _pb2.LMAOEnvelope()
        envelope.ParseFromString(bytes(envelope_bytes))
        which = envelope.WhichOneof("payload")
    except Exception:
        _logger.debug("LMAF: unparsable envelope (%d bytes)", len(envelope_bytes), exc_info=True)
        return None

    try:
        if which == "manifest":
            m = envelope.manifest
            return {
                "payload": "manifest",
                "id": m.id.hex(),
                "payload_sha256": m.payload_sha256.hex(),
                "kind": m.kind,
                "codec": m.codec,
                "chunk_size": m.chunk_size,
                "chunk_count": m.chunk_count,
                "total_bytes": m.total_bytes,
                "sample_rate": m.sample_rate,
                "channels": m.channels,
                "duration_ms": m.duration_ms,
                "width": m.width,
                "height": m.height,
                "node_id": m.node_id,
                "created_ms": m.created_ms,
                "meta": dict(m.meta),
            }
        if which == "chunk":
            c = envelope.chunk
            return {
                "payload": "chunk",
                "id": c.id.hex(),
                "index": c.index,
                "data": bytes(c.data),
                "crc32": c.crc32,
            }
        if which == "att_ack":
            a = envelope.att_ack
            return {
                "payload": "ack",
                "id": a.id.hex(),
                "status": a.status,
                "have_count": a.have_count,
                "missing": list(a.missing),
                "reason": a.reason,
                "retry_after_ms": a.retry_after_ms,
            }
        if which == "caps":
            caps = envelope.caps
            return {
                "payload": "capability",
                "lmaf_version": caps.lmaf_version,
                "kinds": list(caps.kinds),
                "codecs": list(caps.codecs),
                "max_chunk_size": caps.max_chunk_size,
                "max_attachment_bytes": caps.max_attachment_bytes,
                "rx_window": caps.rx_window,
                "airtime_budget_bps": caps.airtime_budget_bps,
            }
    except Exception:
        _logger.debug("LMAF: malformed %s field", which, exc_info=True)
        return None

    if which is None:
        # Parsed, but no payload at all (e.g. an empty envelope).
        return None
    return {"payload": "other"}


def decode_capability(envelope_bytes: bytes) -> dict | None:
    """Decode a ``Capability`` (field 50) envelope, or ``None`` if it is not one."""
    decoded = decode_envelope(envelope_bytes)
    if decoded is None or decoded.get("payload") != "capability":
        return None
    return decoded


def decode_ack(envelope_bytes: bytes) -> dict | None:
    """Decode an ``AttachmentAck`` (field 42) envelope, or ``None`` if it is not one."""
    decoded = decode_envelope(envelope_bytes)
    if decoded is None or decoded.get("payload") != "ack":
        return None
    return decoded
