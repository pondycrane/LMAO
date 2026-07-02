"""Smoke tests for urns LXMessage — pack/unpack round-trip on CPython."""

import sys
import os
import hashlib
from unittest.mock import MagicMock

# Ensure the urns package is importable
_urns_parent = os.path.join(os.path.dirname(__file__), "..", "cardputer_client", "lib")
_urns_parent = os.path.abspath(_urns_parent)
if _urns_parent not in sys.path:
    sys.path.insert(0, _urns_parent)

# Mock MicroPython dependencies
class _MockMicroPython:
    @staticmethod
    def const(x):
        return x

    @staticmethod
    def native(f):
        return f

sys.modules["micropython"] = _MockMicroPython()

_mp_uhashlib = MagicMock()
_mp_uhashlib.sha256 = hashlib.sha256
sys.modules["uhashlib"] = _mp_uhashlib

class _MockAESCipher:
    def __init__(self, key, mode, iv):
        pass

    def encrypt(self, plaintext):
        return plaintext

    def decrypt(self, ciphertext):
        return ciphertext

_mp_ucryptolib = MagicMock()
_mp_ucryptolib.aes = _MockAESCipher
sys.modules["ucryptolib"] = _mp_ucryptolib

from urns.identity import Identity  # noqa: E402
from urns.destination import Destination  # noqa: E402
from urns.lxmf import LXMessage, APP_NAME  # noqa: E402


class TestLXMessageRoundTrip:
    """Round-trip pack / unpack_from_bytes for LXMF messages."""

    def setup_method(self):
        self.source = Identity()
        self.destination = Identity()
        self.dest_obj = Destination(
            self.destination, Destination.OUT, Destination.SINGLE, APP_NAME
        )

    def test_text_message_round_trip(self):
        msg = LXMessage(
            destination=self.dest_obj,
            source=self.source,
            content=b"Hello LXMF",
            title="test.message",
            desired_method=LXMessage.OPPORTUNISTIC,
        )
        msg.pack()
        packed = msg.packed

        # Unpack from bytes
        unpacked = LXMessage.unpack_from_bytes(packed)
        assert unpacked is not None, "Unpack should succeed"
        assert unpacked.content == b"Hello LXMF"

    def test_binary_content_round_trip(self):
        data = bytes(range(256))
        msg = LXMessage(
            destination=self.dest_obj,
            source=self.source,
            content=data,
            title="binary.test",
            desired_method=LXMessage.OPPORTUNISTIC,
        )
        msg.pack()
        unpacked = LXMessage.unpack_from_bytes(msg.packed)
        assert unpacked is not None
        assert unpacked.content == data

    def test_title_as_string(self):
        msg = LXMessage(
            destination=self.dest_obj,
            source=self.source,
            content=b"title test",
            title="p:Envelope",
            desired_method=LXMessage.OPPORTUNISTIC,
        )
        msg.pack()
        unpacked = LXMessage.unpack_from_bytes(msg.packed)
        assert unpacked is not None
        assert unpacked.title_as_string() == "p:Envelope"

    def test_empty_content_round_trip(self):
        msg = LXMessage(
            destination=self.dest_obj,
            source=self.source,
            content=b"",
            title="empty",
            desired_method=LXMessage.OPPORTUNISTIC,
        )
        msg.pack()
        unpacked = LXMessage.unpack_from_bytes(msg.packed)
        assert unpacked is not None
        assert unpacked.content == b""

    def test_unpack_garbage_returns_none(self):
        try:
            result = LXMessage.unpack_from_bytes(b"\x00\x01\x02")
        except Exception:
            result = None
        assert result is None, "Unpacking garbage should return None or raise"

    def test_unpack_empty_bytes_returns_none(self):
        try:
            result = LXMessage.unpack_from_bytes(b"")
        except Exception:
            result = None
        assert result is None
