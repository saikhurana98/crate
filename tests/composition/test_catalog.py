"""Source catalog + credential status — presence only, never values (ADR-0018)."""

import copy

import pytest

from lbmd.composition import catalog
from lbmd.composition.catalog import (
    KNOWN,
    LayoutError,
    default_layout,
    source_status,
    validate_layout,
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for spec in catalog.SOURCES:
        if spec.enable_flag:
            monkeypatch.delenv(spec.enable_flag, raising=False)
        for var in spec.required_env:
            monkeypatch.delenv(var, raising=False)


def _by_name(rows: list[dict[str, object]], name: str) -> dict[str, object]:
    return next(r for r in rows if r["name"] == name)


def test_ytdlp_always_enabled_no_credentials() -> None:
    yt = _by_name(source_status(), "ytdlp")
    assert yt["enabled"] is True
    assert yt["tier"] == 3
    assert yt["credentials"] == "none" and yt["missing_env"] == []


def test_disabled_source_with_missing_credentials() -> None:
    qobuz = _by_name(source_status(), "qobuz")
    assert qobuz["enabled"] is False
    assert qobuz["credentials"] == "missing"
    assert set(qobuz["missing_env"]) == {  # type: ignore[arg-type]
        "LBMD_QOBUZ_APP_ID",
        "LBMD_QOBUZ_APP_SECRET",
        "LBMD_QOBUZ_TOKEN",
    }


def test_enabled_with_full_credentials_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LBMD_QOBUZ", "1")
    monkeypatch.setenv("LBMD_QOBUZ_APP_ID", "id")
    monkeypatch.setenv("LBMD_QOBUZ_APP_SECRET", "secret")
    monkeypatch.setenv("LBMD_QOBUZ_TOKEN", "tok")
    qobuz = _by_name(source_status(), "qobuz")
    assert qobuz["enabled"] is True and qobuz["credentials"] == "ok"


def test_partial_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LBMD_QOBUZ_APP_ID", "id")  # only 1 of 3
    qobuz = _by_name(source_status(), "qobuz")
    assert qobuz["credentials"] == "partial"
    assert qobuz["missing_env"] == ["LBMD_QOBUZ_APP_SECRET", "LBMD_QOBUZ_TOKEN"]


def test_status_never_leaks_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LBMD_DEEZER_ARL", "super-secret-arl-value")
    deezer = _by_name(source_status(), "deezer")
    assert "super-secret-arl-value" not in repr(deezer)
    assert deezer["credentials"] == "ok"


def test_monochrome_has_no_env_secret_but_toggles(monkeypatch: pytest.MonkeyPatch) -> None:
    mono = _by_name(source_status(), "monochrome")
    assert mono["enabled"] is False and mono["credentials"] == "none"
    monkeypatch.setenv("LBMD_MONOCHROME", "yes")
    assert _by_name(source_status(), "monochrome")["enabled"] is True


def test_status_overrides_from_store() -> None:
    # enabled_of + has_secret let the stored layout/secrets drive status
    rows = source_status(
        enabled_of=lambda n: n == "qobuz",
        has_secret=lambda n, k: n == "qobuz",  # all qobuz creds "stored"
    )
    q = _by_name(rows, "qobuz")
    assert q["enabled"] is True and q["credentials"] == "ok"
    assert _by_name(rows, "deezer")["enabled"] is False


def test_default_layout_places_every_source_once() -> None:
    lay = default_layout()
    placed = [n for t in lay["tiers"] for n in t["sources"]] + lay["disabled"]
    assert set(placed) == KNOWN and len(placed) == len(KNOWN)
    t3 = next(t for t in lay["tiers"] if t["tier"] == 3)
    assert "ytdlp" in t3["sources"]
    assert "qobuz" in lay["disabled"]  # no env flag -> tray


def test_validate_accepts_default_layout() -> None:
    assert validate_layout(default_layout())["tiers"]


def test_validate_rejects_incomplete_and_bad_shapes() -> None:
    with pytest.raises(LayoutError):
        validate_layout("nope")
    with pytest.raises(LayoutError):  # missing sources -> not every source placed
        validate_layout(
            {
                "tiers": [{"tier": t, "strategy": "all", "sources": []} for t in (1, 2, 3)],
                "disabled": [],
            }
        )
    lay = default_layout()
    bad_strategy = copy.deepcopy(lay)
    bad_strategy["tiers"][0]["strategy"] = "wat"
    with pytest.raises(LayoutError):
        validate_layout(bad_strategy)


def test_validate_rejects_duplicate_source() -> None:
    lay = default_layout()
    lay["disabled"].append(lay["disabled"][0])  # duplicate
    with pytest.raises(LayoutError):
        validate_layout(lay)


def test_validate_pins_ytdlp_to_tier3() -> None:
    lay = default_layout()
    # move ytdlp from tier 3 to disabled
    for t in lay["tiers"]:
        t["sources"] = [s for s in t["sources"] if s != "ytdlp"]
    lay["disabled"].append("ytdlp")
    with pytest.raises(LayoutError):
        validate_layout(lay)
