"""Tier-cascade domain logic (ADR-0015).

Strict Tier 1 -> 2 -> 3 priority; within a tier, sources race in parallel.
These tests pin the pure planning logic: turn a set of available sources into
an ordered list of race groups.
"""

import pytest

from crate.domain.tiers import SourceRef, Tier, plan_cascade


def test_tier_values_are_priority_ordered() -> None:
    assert Tier.ONE < Tier.TWO < Tier.THREE
    assert [t.value for t in Tier] == [1, 2, 3]


def test_plan_orders_groups_by_tier_ascending() -> None:
    sources = [
        SourceRef("ytdlp", Tier.THREE),
        SourceRef("squid", Tier.ONE),
        SourceRef("spotify", Tier.TWO),
    ]
    plan = plan_cascade(sources)
    assert [[s.name for s in group] for group in plan] == [["squid"], ["spotify"], ["ytdlp"]]


def test_plan_groups_multiple_sources_within_a_tier() -> None:
    sources = [
        SourceRef("monochrome", Tier.ONE),
        SourceRef("squid", Tier.ONE),
        SourceRef("lucida", Tier.ONE),
    ]
    plan = plan_cascade(sources)
    assert len(plan) == 1
    # deterministic ordering within a tier: alphabetical by name
    assert [s.name for s in plan[0]] == ["lucida", "monochrome", "squid"]


def test_plan_excludes_unhealthy_sources() -> None:
    sources = [
        SourceRef("squid", Tier.ONE, healthy=False),
        SourceRef("lucida", Tier.ONE, healthy=True),
    ]
    plan = plan_cascade(sources)
    assert [[s.name for s in group] for group in plan] == [["lucida"]]


def test_plan_skips_tiers_with_no_healthy_sources() -> None:
    sources = [
        SourceRef("squid", Tier.ONE, healthy=False),
        SourceRef("ytdlp", Tier.THREE),
    ]
    plan = plan_cascade(sources)
    # Tier 1 empty (unhealthy), Tier 2 absent -> only Tier 3 group remains
    assert [[s.name for s in group] for group in plan] == [["ytdlp"]]


def test_plan_of_no_sources_is_empty() -> None:
    assert plan_cascade([]) == []


def test_sourceref_is_immutable() -> None:
    ref = SourceRef("squid", Tier.ONE)
    with pytest.raises(AttributeError):
        ref.name = "lucida"  # type: ignore[misc]
