"""AES-GCM secret box for credentials at rest (ADR-0018, Phase 3 groundwork).

The console lets the operator store source credentials; they must not sit in
plaintext on disk. Encrypt with AES-256-GCM (pycryptodome — already a dep). Key
comes from ``LBMD_CONFIG_KEY`` (base64 of 32 bytes); if unset, a random key is
generated once and persisted next to the DB with 0600 perms.
"""

from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path

from Crypto.Cipher import AES

_KEY_ENV = "LBMD_CONFIG_KEY"
_KEY_LEN = 32
_NONCE = 12
_TAG = 16


def _load_key(keyfile: Path) -> bytes:
    env = os.environ.get(_KEY_ENV)
    if env:
        key = base64.b64decode(env)
        if len(key) != _KEY_LEN:
            raise ValueError(f"{_KEY_ENV} must be base64 of exactly {_KEY_LEN} bytes")
        return key
    if keyfile.exists():
        return base64.b64decode(keyfile.read_text().strip())
    key = secrets.token_bytes(32)
    keyfile.parent.mkdir(parents=True, exist_ok=True)
    keyfile.write_text(base64.b64encode(key).decode())
    keyfile.chmod(0o600)
    return key


class SecretBox:
    """Encrypt/decrypt short credential strings. Output = nonce|tag|ciphertext."""

    def __init__(self, key: bytes) -> None:
        if len(key) != _KEY_LEN:
            raise ValueError(f"key must be {_KEY_LEN} bytes")
        self._key = key

    @classmethod
    def from_keyfile(cls, keyfile: Path) -> SecretBox:
        return cls(_load_key(keyfile))

    def encrypt(self, plaintext: str) -> bytes:
        nonce = secrets.token_bytes(_NONCE)
        cipher = AES.new(self._key, AES.MODE_GCM, nonce=nonce)
        ct, tag = cipher.encrypt_and_digest(plaintext.encode())
        return nonce + tag + ct

    def decrypt(self, blob: bytes) -> str:
        nonce, tag, ct = blob[:_NONCE], blob[_NONCE : _NONCE + _TAG], blob[_NONCE + _TAG :]
        cipher = AES.new(self._key, AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(ct, tag).decode()
