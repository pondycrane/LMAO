# µReticulum Cryptography
# Always uses internal provider (no PyCA/OpenSSL on MicroPython)

# Share native crypto module with x25519 (ed25519 loads it from root filesystem)
from . import ed25519 as _ed
from . import x25519 as _x25
from .ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from .hashes import sha256, sha512
from .hkdf import hkdf
from .pkcs7 import PKCS7
from .token import Token
from .x25519 import X25519PrivateKey, X25519PublicKey

_x25._native = _ed._native
del _ed, _x25

PROVIDER_INTERNAL = 0x01
PROVIDER = PROVIDER_INTERNAL


def backend():
    return "internal"
