"""ConfigStore — layout/settings/secret persistence (ADR-0018, Phase 2)."""

from pathlib import Path

from crate.infrastructure.crypto import SecretBox
from crate.infrastructure.sqlite.config_store import ConfigStore
from crate.infrastructure.sqlite.db import connect


def _store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(connect(tmp_path / "crate.db"), SecretBox(bytes(range(32))))


def test_layout_absent_then_seed_then_put_bumps_version(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert s.get_layout() is None
    assert s.version() == 0

    layout = {"tiers": [{"tier": 1, "strategy": "all", "sources": ["qobuz"]}], "disabled": []}
    s.seed_layout(layout)
    assert s.get_layout() == layout
    assert s.version() == 1

    s.seed_layout({"tiers": [], "disabled": []})  # INSERT OR IGNORE — no-op
    assert s.get_layout() == layout

    v = s.put_layout(
        {"tiers": [{"tier": 2, "strategy": "uniform", "sources": []}], "disabled": ["qobuz"]}
    )
    assert v == 2 and s.version() == 2
    assert s.get_layout()["disabled"] == ["qobuz"]


def test_put_layout_first_write_is_version_1(tmp_path: Path) -> None:
    assert _store(tmp_path).put_layout({"tiers": [], "disabled": []}) == 1


def test_settings_upsert(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert s.get_settings("soulseek") == {}
    s.put_settings("soulseek", {"min_bitrate_kbps": 256})
    assert s.get_settings("soulseek") == {"min_bitrate_kbps": 256}
    s.put_settings("soulseek", {"min_bitrate_kbps": 320})
    assert s.get_settings("soulseek")["min_bitrate_kbps"] == 320


def test_secrets_encrypted_at_rest_and_status(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert s.secret_keys("qobuz") == set()
    s.put_secret("qobuz", "CRATE_QOBUZ_TOKEN", "tok-123")
    s.put_secret("qobuz", "CRATE_QOBUZ_APP_ID", "app-9")
    assert s.secret_keys("qobuz") == {"CRATE_QOBUZ_TOKEN", "CRATE_QOBUZ_APP_ID"}
    assert s.get_secret("qobuz", "CRATE_QOBUZ_TOKEN") == "tok-123"
    assert s.get_secret("qobuz", "NOPE") is None
    # ciphertext on disk never contains the plaintext
    row = s._conn.execute(
        "SELECT ciphertext FROM source_secret WHERE env_key='CRATE_QOBUZ_TOKEN'"
    ).fetchone()
    assert b"tok-123" not in row["ciphertext"]


def test_secret_replace(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.put_secret("deezer", "CRATE_DEEZER_ARL", "old")
    s.put_secret("deezer", "CRATE_DEEZER_ARL", "new")
    assert s.get_secret("deezer", "CRATE_DEEZER_ARL") == "new"
    assert s.secret_keys("deezer") == {"CRATE_DEEZER_ARL"}
