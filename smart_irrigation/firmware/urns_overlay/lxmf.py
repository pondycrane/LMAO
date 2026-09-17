# µReticulum LXMF — Sprout send-only overlay
# Wire-compatible with reference LXMF and the canonical urns lxmf.py, but
# REDUCED for the ESP32-PICO-D4's tiny heap: only LXMessage (pack + send) and a
# minimal opportunistic send helper. No LXMRouter, no receive/receipts, no
# resource/link delivery — the Sprout node only needs to SEND SensorReports.
#
# This file is used at build time by firmware/tools/build_mpy.sh to OVERRIDE
# the canonical urns/lxmf.mpy on the Sprout device; the canonical
# cardputer_client/lib/urns/lxmf.py is untouched (single source of truth).

import sys
import time

from . import umsgpack
from .crypto.hashes import sha256
from .destination import Destination
from .identity import Identity
from .log import LOG_DEBUG, LOG_ERROR, LOG_NOTICE, LOG_VERBOSE, log
from .packet import Packet

APP_NAME = "lxmf"


class LXMessage:
    """LXMF message — send-only surface, wire-compatible with reference."""

    # States
    GENERATING = 0x00
    OUTBOUND = 0x01
    SENDING = 0x02
    SENT = 0x04
    DELIVERED = 0x08
    FAILED = 0xFF

    # Delivery methods
    UNKNOWN = 0x00
    OPPORTUNISTIC = 0x01
    DIRECT = 0x02
    PROPAGATED = 0x03

    # Representation
    PACKET = 0x01
    RESOURCE = 0x02

    # Verification
    SOURCE_UNKNOWN = 0x01
    SIGNATURE_INVALID = 0x02

    # Sizes
    DESTINATION_LENGTH = Identity.TRUNCATED_HASHLENGTH // 8  # 16
    SIGNATURE_LENGTH = Identity.SIGLENGTH // 8  # 64
    TIMESTAMP_SIZE = 8
    STRUCT_OVERHEAD = 8
    LXMF_OVERHEAD = (
        2 * DESTINATION_LENGTH + SIGNATURE_LENGTH + TIMESTAMP_SIZE + STRUCT_OVERHEAD
    )  # 112

    ENCRYPTED_PACKET_MAX_CONTENT = (
        Packet.ENCRYPTED_MDU + TIMESTAMP_SIZE - LXMF_OVERHEAD + DESTINATION_LENGTH
    )

    def __init__(
        self,
        destination=None,
        source=None,
        content=b"",
        title=b"",
        fields=None,
        desired_method=None,
    ):
        self._destination = destination
        self._source = source
        self.destination_hash = destination.hash if destination else None
        self.source_hash = source.hash if source else None

        if isinstance(title, str):
            title = title.encode("utf-8")
        if isinstance(content, str):
            content = content.encode("utf-8")

        self.title = title
        self.content = content
        self.fields = fields if fields is not None else {}
        self.desired_method = desired_method

        self.timestamp = None
        self.signature = None
        self.hash = None
        self.message_id = None
        self.packed = None
        self.state = LXMessage.GENERATING
        self.method = LXMessage.UNKNOWN
        self.transport_encrypted = False
        self.transport_encryption = None
        self._delivery_callback = None
        self._failed_callback = None

    def pack(self):
        """Pack message into the LXMF wire format."""
        if self.packed:
            raise ValueError("Message already packed")

        if self.timestamp is None:
            platform = sys.platform
            if platform == "esp32":
                # ESP32 MicroPython epoch is 2000-01-01; add 946,684,800 for Unix.
                self.timestamp = 946684800 + time.time()
            else:
                self.timestamp = time.time()

        payload = [self.timestamp, self.title, self.content, self.fields]

        hashed_part = self._destination.hash
        hashed_part += self._source.hash
        hashed_part += umsgpack.packb(payload)
        self.hash = sha256(hashed_part)
        self.message_id = self.hash

        self.signature = self._source.sign(hashed_part + self.hash)
        self.signature_validated = True

        packed_payload = umsgpack.packb(payload)
        self.packed = (
            self._destination.hash + self._source.hash + self.signature + packed_payload
        )

        if self.desired_method is None and hasattr(self, "desired_method"):
            self.desired_method = LXMessage.OPPORTUNISTIC
        self.method = self.desired_method

    def send(self):
        """Send opportunistically (single RNS packet) — Sprout's path."""
        if not self.packed:
            self.pack()
        data = self.packed[self.DESTINATION_LENGTH :]
        pkt = Packet(self._destination, data)
        pkt.send()
        self.state = LXMessage.SENT
        self.transport_encrypted = True
        self.transport_encryption = "Curve25519"
        log(
            "Sent opportunistic LXMF to " + self.destination_hash.hex()[:8],
            LOG_NOTICE,
        )
        if self._delivery_callback:
            try:
                self._delivery_callback(self)
            except Exception as e:
                log("Delivery callback error: " + str(e), LOG_ERROR)


def send_opportunistic(destination_hash, identity, content, title=b"", fields=None):
    """Send *content* as an LXMF message to *destination_hash* opportunistically.

    Mirrors LXMRouter.send_message's delivery chain in send-only form:
      1. if a path to the destination is known -> build + pack + send now,
      2. else request the path; the server's announce teaches us its identity
         and the send fires from the on_found callback (run the RNS event loop
         to let that happen).
    Returns the LXMessage when sent, True when queued pending a path, or None
    on hard failure.
    """
    from .transport import Transport

    def _build_and_send():
        dest_identity = Identity.recall(destination_hash)
        if dest_identity is None:
            log(
                "LXMF delivery identity unknown for "
                + bytes(destination_hash).hex()[:8],
                LOG_ERROR,
            )
            return None
        dest = Destination(
            dest_identity, Destination.OUT, Destination.SINGLE, APP_NAME, "delivery"
        )
        src = Destination(
            identity, Destination.OUT, Destination.SINGLE, APP_NAME, "delivery"
        )
        msg = LXMessage(
            destination=dest, source=src, content=content, title=title, fields=fields
        )
        msg.send()
        return msg

    if not Transport.has_path(destination_hash):
        log(
            "LXMF no path to " + bytes(destination_hash).hex()[:8] + "; requesting",
            LOG_VERBOSE,
        )
        try:
            Transport.ensure_path(
                destination_hash,
                on_found=_build_and_send,
                on_timeout=lambda: log("LXMF path timeout", LOG_ERROR),
            )
            return True  # queued
        except Exception as e:
            log("LXMF ensure_path failed: " + str(e), LOG_ERROR)
            return None
    return _build_and_send()
