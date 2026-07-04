"""Declarative source catalog + credential *status* (never values).

The console needs to show, per source: which tier it sits in, whether it's
enabled, and whether its credentials are present — without ever reading or
exposing the secret values. This is also the seed for Phase 2 (drag-drop tier
placement) and Phase 3 (encrypted credential editing).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SourceSpec:
    name: str
    tier: int
    enable_flag: str | None  # env flag that opts the source in; None => always on
    required_env: tuple[str, ...]  # secrets/config needed to actually run it
    lossless: bool


# Mirrors composition.sources.build_tiers() wiring.
SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec(
        "qobuz",
        1,
        "LBMD_QOBUZ",
        ("LBMD_QOBUZ_APP_ID", "LBMD_QOBUZ_APP_SECRET", "LBMD_QOBUZ_TOKEN"),
        True,
    ),
    SourceSpec("monochrome", 1, "LBMD_MONOCHROME", (), True),  # browser session, no env secret
    SourceSpec("deezer", 2, "LBMD_DEEZER", ("LBMD_DEEZER_ARL",), True),
    SourceSpec(
        "soulseek",
        2,
        "LBMD_SOULSEEK",
        ("LBMD_SLSKD_URL", "LBMD_SLSKD_API_KEY", "LBMD_SLSKD_DOWNLOADS"),
        False,
    ),
    SourceSpec(
        "telegram",
        2,
        "LBMD_TELEGRAM",
        ("LBMD_TG_API_ID", "LBMD_TG_API_HASH", "LBMD_TG_SESSION", "LBMD_TG_BOT"),
        False,
    ),
    SourceSpec("ytdlp", 3, None, (), False),
)


def _enabled(flag: str | None) -> bool:
    if flag is None:
        return True  # always-on (ytdlp)
    return os.environ.get(flag, "").lower() in ("1", "true", "yes")


HasSecret = Callable[[str, str], bool]  # (source_name, env_key) -> stored?


def _credentials(spec: SourceSpec, has_secret: HasSecret | None) -> tuple[str, list[str]]:
    """Return (status, missing_env). status is ok | missing | partial | none.

    A required env var counts as present if it's set in the environment OR held
    (encrypted) in the config store — so credentials saved via the console show
    as configured without ever reading their values here."""
    if not spec.required_env:
        return "none", []

    def present(env: str) -> bool:
        if os.environ.get(env):
            return True
        return has_secret is not None and has_secret(spec.name, env)

    missing = [v for v in spec.required_env if not present(v)]
    if not missing:
        return "ok", []
    if len(missing) == len(spec.required_env):
        return "missing", missing
    return "partial", missing


def source_status(
    enabled_of: Callable[[str], bool] | None = None,
    has_secret: HasSecret | None = None,
) -> list[dict[str, object]]:
    """One status record per source. Reports presence only — never secret values.

    ``enabled_of`` overrides the env-flag enable check with the stored layout's
    membership (a source is enabled when it sits in some tier). ``has_secret``
    lets stored credentials count toward 'configured'."""
    out: list[dict[str, object]] = []
    for spec in SOURCES:
        status, missing = _credentials(spec, has_secret)
        enabled = enabled_of(spec.name) if enabled_of is not None else _enabled(spec.enable_flag)
        out.append(
            {
                "name": spec.name,
                "tier": spec.tier,
                "enabled": enabled,
                "lossless": spec.lossless,
                "credentials": status,
                "missing_env": missing,
                "required_env": list(spec.required_env),
            }
        )
    return out


# --- runtime layout (Phase 2) ---

PINNED: frozenset[str] = frozenset({"ytdlp"})  # must stay in Tier 3, always enabled
KNOWN: frozenset[str] = frozenset(s.name for s in SOURCES)
TIER_OF: dict[str, int] = {s.name: s.tier for s in SOURCES}  # source -> its catalog tier
_TIERS: tuple[int, ...] = (1, 2, 3)
_STRATEGIES = frozenset({"all", "uniform"})


class LayoutError(ValueError):
    """A proposed cascade layout is invalid."""


def default_layout() -> dict[str, Any]:
    """Seed layout from current env state: enabled sources sit in their catalog
    tier, the rest in the disabled tray. Strategy defaults to 'all'."""
    by_tier: dict[int, list[str]] = {1: [], 2: [], 3: []}
    disabled: list[str] = []
    for spec in SOURCES:
        if spec.name in PINNED or _enabled(spec.enable_flag):
            by_tier[spec.tier].append(spec.name)
        else:
            disabled.append(spec.name)
    return {
        "tiers": [{"tier": t, "strategy": "all", "sources": by_tier[t]} for t in (1, 2, 3)],
        "disabled": disabled,
    }


def validate_layout(layout: Any) -> dict[str, Any]:
    """Validate a proposed layout; return it normalised. Raise LayoutError on any
    structural problem, unknown/duplicate source, or a pinned-source violation."""
    if not isinstance(layout, dict):
        raise LayoutError("layout must be an object")
    tiers = layout.get("tiers")
    disabled = layout.get("disabled", [])
    if not isinstance(tiers, list) or not isinstance(disabled, list):
        raise LayoutError("layout.tiers and layout.disabled must be lists")
    seen: list[str] = []
    tier_nums: list[int] = []
    norm_tiers: list[dict[str, Any]] = []
    for t in tiers:
        if not isinstance(t, dict) or t.get("tier") not in _TIERS:
            raise LayoutError("each tier needs a tier number in 1..3")
        strategy = t.get("strategy", "all")
        if strategy not in _STRATEGIES:
            raise LayoutError(f"strategy must be one of {sorted(_STRATEGIES)}")
        sources = t.get("sources", [])
        if not isinstance(sources, list):
            raise LayoutError("tier.sources must be a list")
        tier_nums.append(int(t["tier"]))
        seen.extend(sources)
        norm_tiers.append({"tier": int(t["tier"]), "strategy": strategy, "sources": list(sources)})
    if sorted(tier_nums) != list(_TIERS):
        raise LayoutError("layout must define tiers 1, 2 and 3 exactly once")
    seen.extend(disabled)
    if any(name not in KNOWN for name in seen):
        raise LayoutError("layout references an unknown source")
    if len(seen) != len(set(seen)):
        raise LayoutError("a source appears more than once")
    if set(seen) != KNOWN:
        raise LayoutError("layout must place every source exactly once")
    tier3 = next(t for t in norm_tiers if t["tier"] == _TIERS[-1])
    for pin in PINNED:
        if pin not in tier3["sources"]:
            raise LayoutError(f"{pin} is pinned to Tier 3 and cannot be moved or disabled")
    return {"tiers": norm_tiers, "disabled": list(disabled)}
