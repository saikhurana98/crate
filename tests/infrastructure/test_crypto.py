"""SecretBox — AES-GCM credential encryption (ADR-0018)."""

import base64

import pytest

from lbmd.infrastructure.crypto import SecretBox


def test_encrypt_roundtrip() -> None:
    box = SecretBox(bytes(range(32)))
    blob = box.encrypt("qobuz-token-abc123")
    assert isinstance(blob, bytes) and b"qobuz-token" not in blob
    assert box.decrypt(blob) == "qobuz-token-abc123"


def test_two_encryptions_differ_but_decrypt_same() -> None:
    box = SecretBox(bytes(range(32)))
    assert box.encrypt("x") != box.encrypt("x")  # random nonce
    assert box.decrypt(box.encrypt("héllo·世界")) == "héllo·世界"


def test_tamper_is_rejected() -> None:
    box = SecretBox(bytes(range(32)))
    blob = bytearray(box.encrypt("secret"))
    blob[-1] ^= 0x01
    with pytest.raises(ValueError):
        box.decrypt(bytes(blob))


def test_bad_key_length() -> None:
    with pytest.raises(ValueError):
        SecretBox(b"too-short")


def test_from_keyfile_uses_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LBMD_CONFIG_KEY", base64.b64encode(bytes(range(32))).decode())
    kf = tmp_path / "config.key"
    box = SecretBox.from_keyfile(kf)
    assert box.decrypt(box.encrypt("a")) == "a"
    assert not kf.exists()  # env key used, no file written


def test_from_keyfile_generates_and_reuses(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("LBMD_CONFIG_KEY", raising=False)
    kf = tmp_path / "config.key"
    b1 = SecretBox.from_keyfile(kf)
    assert kf.exists() and (kf.stat().st_mode & 0o777) == 0o600
    blob = b1.encrypt("z")
    assert SecretBox.from_keyfile(kf).decrypt(blob) == "z"  # same persisted key


def test_from_keyfile_rejects_wrong_env_length(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LBMD_CONFIG_KEY", base64.b64encode(b"short").decode())
    with pytest.raises(ValueError):
        SecretBox.from_keyfile(tmp_path / "config.key")
