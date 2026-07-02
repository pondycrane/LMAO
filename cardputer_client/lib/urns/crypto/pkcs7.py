"""
µReticulum PKCS7 padding for AES-CBC.

Blocksize defaults to 16 bytes (AES block size).
Padding is deterministic: value = number of pad bytes (1-16).
Unpadded data with an invalid padding value raises ``ValueError``.
"""


class PKCS7:
    BLOCKSIZE = 16

    @staticmethod
    def pad(data, bs=BLOCKSIZE):
        n = bs - len(data) % bs
        return data + bytes([n]) * n

    @staticmethod
    def unpad(data, bs=BLOCKSIZE):
        n = data[-1]
        if n > bs:
            raise ValueError("Invalid padding length: " + str(n))
        return data[:len(data) - n]
