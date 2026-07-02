"""Smoke tests for urns Identity — key generation, encrypt/decrypt, sign/validate."""

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

from urns import const  # noqa: E402
from urns.identity import Identity  # noqa: E402


class TestIdentityCreation:
    """Identity creation and basic properties."""

    def test_creates_valid_keypair(self):
        identity = Identity()
        assert identity.hash is not None
        assert len(identity.hash) == const.TRUNCATED_HASHLENGTH // 8  # 16 bytes (truncated)
        assert identity.pub is not None
        assert identity.prv is not None

    def test_two_identities_have_different_hashes(self):
        id1 = Identity()
        id2 = Identity()
        assert id1.hash != id2.hash, "Two identities must have distinct hashes"


class TestEncryptDecrypt:
    """Encrypt / decrypt round-trip."""

    def test_round_trip_short_message(self):
        identity = Identity()
        plaintext = b"hello"
        ciphertext = identity.encrypt(plaintext)
        assert ciphertext != plaintext
        decrypted = identity.decrypt(ciphertext)
        assert decrypted == plaintext

    def test_round_trip_long_message(self):
        identity = Identity()
        plaintext = b"x" * 200
        ciphertext = identity.encrypt(plaintext)
        decrypted = identity.decrypt(ciphertext)
        assert decrypted == plaintext

    def test_round_trip_empty_message(self):
        identity = Identity()
        plaintext = b""
        ciphertext = identity.encrypt(plaintext)
        decrypted = identity.decrypt(ciphertext)
        assert decrypted == plaintext

    def test_round_trip_binary_message(self):
        identity = Identity()
        plaintext = bytes(range(256))
        ciphertext = identity.encrypt(plaintext)
        decrypted = identity.decrypt(ciphertext)
        assert decrypted == plaintext


class TestSignValidate:
    """Sign / validate round-trip."""

    def test_sign_and_validate(self):
        identity = Identity()
        message = b"sign this data"
        signature = identity.sign(message)
        assert identity.validate(signature, message) is True

    def test_tampered_message_fails(self):
        identity = Identity()
        message = b"original message"
        signature = identity.sign(message)
        assert identity.validate(signature, b"tampered message") is False

    def test_tampered_signature_fails(self):
        identity = Identity()
        message = b"test data"
        signature = identity.sign(message)
        bad_signature = b"\x00" * len(signature)
        assert identity.validate(bad_signature, message) is False

    def test_different_identity_signature_fails(self):
        id1 = Identity()
        id2 = Identity()
        message = b"cross-sign test"
        signature = id1.sign(message)
        assert id2.validate(signature, message) is False
