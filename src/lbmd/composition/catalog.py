"""Declarative source catalog + credential *status* (never values).

The console needs to show, per source: which tier it sits in, whether it's
enabled, and whether its credentials are present — without ever reading or
exposing the secret values. This is also the seed for Phase 2 (drag-drop tier
placement) and Phase 3 (encrypted credential editing).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


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


def _credentials(spec: SourceSpec) -> tuple[str, list[str]]:
    """Return (status, missing_env). status is ok | missing | partial | none."""
    if not spec.required_env:
        return "none", []
    missing = [v for v in spec.required_env if not os.environ.get(v)]
    if not missing:
        return "ok", []
    if len(missing) == len(spec.required_env):
        return "missing", missing
    return "partial", missing


def source_status() -> list[dict[str, object]]:
    """One status record per source. Reports presence only — never secret values."""
    out: list[dict[str, object]] = []
    for spec in SOURCES:
        status, missing = _credentials(spec)
        out.append(
            {
                "name": spec.name,
                "tier": spec.tier,
                "enabled": _enabled(spec.enable_flag),
                "lossless": spec.lossless,
                "credentials": status,
                "missing_env": missing,
                "required_env": list(spec.required_env),
            }
        )
    return out
