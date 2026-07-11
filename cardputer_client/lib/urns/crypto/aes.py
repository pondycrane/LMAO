"""
µReticulum AES — CBC-mode encryption via MicroPython's ucryptolib.

Supported key sizes:
  - AES-128-CBC (16-byte key) via ``AES_128_CBC``
  - AES-256-CBC (32-byte key) via ``AES_256_CBC``

Relies on ``ucryptolib`` (hardware-accelerated AES on ESP32).
Only CBC mode is implemented — no GCM, CTR, or other AEAD modes.

Module-level constant ``AES = "AES"`` is provided for Token.py
compatibility (auto-detects key size to choose 128 vs 256).
"""

from ucryptolib import aes as _aes_impl

_MODE_CBC = 2


class AES_128_CBC:
    @staticmethod
    def encrypt(plaintext, key, iv):
        if len(key) != 16:
            raise ValueError("AES-128 key must be 16 bytes")
        cipher = _aes_impl(key, _MODE_CBC, iv)
        return cipher.encrypt(plaintext)

    @staticmethod
    def decrypt(ciphertext, key, iv):
        if len(key) != 16:
            raise ValueError("AES-128 key must be 16 bytes")
        cipher = _aes_impl(key, _MODE_CBC, iv)
        return cipher.decrypt(ciphertext)


class AES_256_CBC:
    @staticmethod
    def encrypt(plaintext, key, iv):
        if len(key) != 32:
            raise ValueError("AES-256 key must be 32 bytes")
        cipher = _aes_impl(key, _MODE_CBC, iv)
        return cipher.encrypt(plaintext)

    @staticmethod
    def decrypt(ciphertext, key, iv):
        if len(key) != 32:
            raise ValueError("AES-256 key must be 32 bytes")
        cipher = _aes_impl(key, _MODE_CBC, iv)
        return cipher.decrypt(ciphertext)


# Module-level constant for Token.py compatibility
AES = "AES"
