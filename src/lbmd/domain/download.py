"""Domain result type for a completed source download.

Shared by every source adapter (Tier 1/2/3) and the cascade use-case, so it
lives in the domain rather than in any one adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DownloadOutcome:
    path: Path
    tier: int
    source: str
    elapsed_s: float
    query: str
