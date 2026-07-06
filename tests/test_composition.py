"""Composition config helpers: enable flags + per-tier mux strategy selection."""

from crate.application.multiplex import MuxStrategy
from crate.composition.sources import _enabled, _strategy


def test_enabled_accepts_truthy_values(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for val in ("1", "true", "YES"):
        monkeypatch.setenv("CRATE_X", val)
        assert _enabled("CRATE_X") is True
    monkeypatch.setenv("CRATE_X", "0")
    assert _enabled("CRATE_X") is False
    monkeypatch.delenv("CRATE_X", raising=False)
    assert _enabled("CRATE_X") is False


def test_strategy_defaults_to_all(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("CRATE_MUX_TIER1", raising=False)
    assert _strategy(1) is MuxStrategy.ALL


def test_strategy_uniform_from_uniform_or_distribute(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CRATE_MUX_TIER2", "uniform")
    assert _strategy(2) is MuxStrategy.UNIFORM
    monkeypatch.setenv("CRATE_MUX_TIER2", "distribute")
    assert _strategy(2) is MuxStrategy.UNIFORM
    monkeypatch.setenv("CRATE_MUX_TIER2", "all")
    assert _strategy(2) is MuxStrategy.ALL
