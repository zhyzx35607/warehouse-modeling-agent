"""SM4 encryption/decryption for database passwords.

SM4 (国密SMS4) is the Chinese national block cipher standard.
128-bit block, 128-bit key, CBC mode with random IV.

Usage:
    from wren_agent.tools.sm4_crypto import sm4_encrypt, sm4_decrypt
    ciphertext = sm4_encrypt("my_password")
    password = sm4_decrypt(ciphertext)
"""

from __future__ import annotations

import base64
import os
from gmssl.sm4 import CryptSM4, SM4_ENCRYPT, SM4_DECRYPT

from wren_agent.config import settings

# SM4 key must be exactly 16 bytes (128 bits)
_KEY_BYTES: bytes | None = None


def _get_key() -> bytes:
    """Get the SM4 key from settings, ensuring exact 16 bytes."""
    global _KEY_BYTES
    if _KEY_BYTES is not None:
        return _KEY_BYTES

    key_str = settings.sm4.key
    if not key_str:
        raise ValueError("SM4_KEY not set in .env — cannot encrypt/decrypt")

    key_bytes = key_str.encode("utf-8")

    # Pad or truncate to exactly 16 bytes
    if len(key_bytes) < 16:
        key_bytes = key_bytes.ljust(16, b'\x00')
    elif len(key_bytes) > 16:
        key_bytes = key_bytes[:16]

    _KEY_BYTES = key_bytes
    return _KEY_BYTES


def sm4_encrypt(plaintext: str) -> str:
    """Encrypt a plaintext password using SM4-CBC.

    Returns base64(IV + ciphertext) — 16 bytes IV prepended.
    """
    if not plaintext:
        return ""

    key = _get_key()
    iv = os.urandom(16)

    crypt_sm4 = CryptSM4()
    crypt_sm4.set_key(key, SM4_ENCRYPT)

    # PKCS7 padding
    data = plaintext.encode("utf-8")
    pad_len = 16 - (len(data) % 16)
    data = data + bytes([pad_len] * pad_len)

    cipher = crypt_sm4.crypt_cbc(iv, data)

    # Store IV + ciphertext as base64
    return base64.b64encode(iv + cipher).decode("ascii")


def sm4_decrypt(encoded: str) -> str:
    """Decrypt an SM4-CBC encrypted password.

    Expects base64(IV + ciphertext).
    """
    if not encoded:
        return ""

    key = _get_key()

    try:
        raw = base64.b64decode(encoded)
        iv = raw[:16]
        cipher = raw[16:]

        crypt_sm4 = CryptSM4()
        crypt_sm4.set_key(key, SM4_DECRYPT)

        plain = crypt_sm4.crypt_cbc(iv, cipher)

        # Remove PKCS7 padding
        pad_len = plain[-1]
        if pad_len < 1 or pad_len > 16:
            raise ValueError("invalid padding")
        plain = plain[:-pad_len]

        return plain.decode("utf-8")
    except Exception:
        raise ValueError("Decryption failed — wrong key or corrupted data")
