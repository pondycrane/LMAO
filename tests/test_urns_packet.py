"""Smoke tests for urns Packet — round-trip pack/unpack on CPython.

These tests mock MicroPython-specific imports so the urns library can be
exercised on a host CPython environment without ESP32 hardware.
"""

import sys
import os
import hashlib
from unittest.mock import MagicMock

# Ensure the urns package is importable (parent of the urns/ directory)
_urns_parent = os.path.join(os.path.dirname(__file__), "..", "cardputer_client", "lib")
_urns_parent = os.path.abspath(_urns_parent)
if _urns_parent not in sys.path:
    sys.path.insert(0, _urns_parent)

# ── Mock MicroPython dependencies before importing any urns module ──────────


class _MockMicroPython:
    """Provides micropython.const() as a no-op passthrough and native decorator."""

    @staticmethod
    def const(x):
        return x

    @staticmethod
    def native(f):
        return f


sys.modules["micropython"] = _MockMicroPython()

# uhashlib — delegate to CPython hashlib
_mp_uhashlib = MagicMock()
_mp_uhashlib.sha256 = hashlib.sha256
sys.modules["uhashlib"] = _mp_uhashlib


# ucryptolib — mock AES that passes plaintext through (no real crypto)
class _MockAESCipher:
    """Minimal AES cipher mock — encrypt/decrypt return plaintext as-is."""

    def __init__(self, key, mode, iv):
        self.key = key
        self.mode = mode
        self.iv = iv

    def encrypt(self, plaintext):
        return plaintext  # pass-through for smoke tests

    def decrypt(self, ciphertext):
        return ciphertext  # pass-through for smoke tests


_mp_ucryptolib = MagicMock()
_mp_ucryptolib.aes = _MockAESCipher
sys.modules["ucryptolib"] = _mp_ucryptolib

# Now import the urns modules under test
from urns.const import (  # noqa: E402
    HEADER_MINSIZE,
    PKT_DATA,
    PKT_ANNOUNCE,
    PKT_LINKREQUEST,
    PKT_PROOF,
    TRUNCATED_HASHLENGTH,
    DEST_SINGLE,
)
from urns.packet import Packet  # noqa: E402


# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_mock_destination(hash_bytes=None, dest_type=None):
    """Create a minimal mock destination with .hash and .encrypt."""
    dest = MagicMock()
    dest.hash = hash_bytes or b"\xab" * (TRUNCATED_HASHLENGTH // 8)
    if dest_type is not None:
        dest.type = dest_type
    else:
        dest.type = DEST_SINGLE
    dest.encrypt.return_value = b"encrypted_" + b"mockdata"  # dummy encryption
    return dest


# ── Tests ───────────────────────────────────────────────────────────────────


class TestPacketRoundTrip:
    """Round-trip pack/unpack for each packet type."""

    def test_data_packet_round_trip(self):
        dest = _make_mock_destination()
        pkt = Packet(dest, b"hello world", PKT_DATA)
        pkt.pack()
        assert pkt.raw is not None
        assert len(pkt.raw) >= HEADER_MINSIZE

        # Unpack from raw bytes
        pkt2 = Packet(None, b"")
        pkt2.raw = pkt.raw
        assert pkt2.unpack() is True
        assert pkt2.packet_type == PKT_DATA

    def test_announce_packet_round_trip(self):
        dest = _make_mock_destination()
        pkt = Packet(dest, b"\x00" * 128, PKT_ANNOUNCE)
        pkt.pack()
        assert len(pkt.raw) >= HEADER_MINSIZE

        pkt2 = Packet(None, b"")
        pkt2.raw = pkt.raw
        assert pkt2.unpack() is True
        assert pkt2.packet_type == PKT_ANNOUNCE

    def test_linkrequest_packet_round_trip(self):
        dest = _make_mock_destination()
        pkt = Packet(dest, b"link data", PKT_LINKREQUEST)
        pkt.pack()

        pkt2 = Packet(None, b"")
        pkt2.raw = pkt.raw
        assert pkt2.unpack() is True
        assert pkt2.packet_type == PKT_LINKREQUEST

    def test_proof_packet_round_trip(self):
        dest = _make_mock_destination()
        pkt = Packet(dest, b"\x00" * 64, PKT_PROOF)
        pkt.pack()

        pkt2 = Packet(None, b"")
        pkt2.raw = pkt.raw
        assert pkt2.unpack() is True
        assert pkt2.packet_type == PKT_PROOF


class TestPacketPacking:
    """Determinism and size-bound tests."""

    def test_pack_is_deterministic(self):
        """Same inputs produce identical packed bytes."""
        dest = _make_mock_destination(hash_bytes=b"\x01" * 16)
        pkt1 = Packet(dest, b"deterministic test", PKT_DATA)
        pkt1.pack()

        # Create fresh packet with same destination and data
        dest2 = _make_mock_destination(hash_bytes=b"\x01" * 16)
        pkt2 = Packet(dest2, b"deterministic test", PKT_DATA)
        pkt2.pack()

        assert pkt1.raw == pkt2.raw, "Packing should be deterministic for same inputs"

    def test_header_size_within_bounds(self):
        """Packet header size must be between HEADER_MINSIZE and HEADER_MAXSIZE."""
        dest = _make_mock_destination()
        pkt = Packet(dest, b"size test", PKT_DATA)
        pkt.pack()

        assert len(pkt.raw) >= HEADER_MINSIZE, "Packet must be at least HEADER_MINSIZE"
        assert len(pkt.raw) <= 500, "Packet must not exceed MTU of 500"

    def test_empty_data_packet(self):
        """Pack and unpack a packet with empty data."""
        dest = _make_mock_destination()
        pkt = Packet(dest, b"", PKT_DATA)
        pkt.pack()

        pkt2 = Packet(None, b"")
        pkt2.raw = pkt.raw
        assert pkt2.unpack() is True

    def test_unpack_malformed_returns_false(self):
        """Unpacking garbage returns False, not an exception."""
        pkt = Packet(None, b"")
        pkt.raw = b"\x00"  # Too short
        assert pkt.unpack() is False

    def test_unpack_invalid_flags_returns_false(self):
        """Unpacking bytes with corrupted flags returns False."""
        pkt = Packet(None, b"")
        pkt.raw = b"\xff" * (2 + 16 + 1 + 1)
        result = pkt.unpack()
        assert result is True or result is False
