"""compute_listening_tiers / approximate_tiers / diff_members."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from crate.application.affinity import (
    TrackPlays,
    approximate_tiers,
    compute_listening_tiers,
    diff_members,
    spotify_id_of,
)
from crate.domain.affinity import AffinityThresholds, AffinityTier

NOW = datetime(2026, 9, 14, tzinfo=UTC)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def plays(start: datetime, n: int, every_days: int) -> list[str]:
    return [iso(start + timedelta(days=i * every_days)) for i in range(n)]


def constant(key: str, uri: str | None = "spotify:track:c1") -> TrackPlays:
    times = [t for q in range(4) for t in plays(datetime(2022, 1 + 3 * q, 1, tzinfo=UTC), 4, 5)]
    return TrackPlays(key, times, "Artist", key, uri)


def fling(key: str, n: int = 10) -> TrackPlays:
    return TrackPlays(
        key, plays(datetime(2024, 3, 1, tzinfo=UTC), n, 2), "Artist", key, "spotify:track:f1"
    )


def test_tiers_split_rank_and_carry_stats() -> None:
    rising = TrackPlays("tok:rising", plays(NOW - timedelta(days=20), 9, 2), "A", "Rising", None)
    tiers = compute_listening_tiers(
        [
            constant("tok:c"),
            fling("tok:f-big", 12),
            fling("tok:f-small", 9),
            rising,
            TrackPlays("tok:once", plays(NOW, 1, 1), "A", "Once", None),
            TrackPlays("tok:empty", [], "A", "Empty", None),
        ],
        AffinityThresholds(),
        NOW,
    )
    assert [m.merge_key for m in tiers.constants] == ["tok:c"]
    assert [m.merge_key for m in tiers.flings] == ["tok:f-big", "tok:f-small"]  # by burst size
    assert [m.merge_key for m in tiers.rising] == ["tok:rising"]
    c = tiers.constants[0]
    assert c.tier is AffinityTier.CONSTANT and c.spotify_id == "c1"
    assert c.stats["plays"] == 16 and c.stats["span"] == "Q1'22-Q4'22" and c.stats["local"] is False
    assert tiers.rising[0].stats["local"] is True and tiers.approximate is False


def test_local_files_have_no_spotify_id() -> None:
    tiers = compute_listening_tiers([constant("tok:local", uri=None)], AffinityThresholds(), NOW)
    assert tiers.constants[0].spotify_id is None and tiers.constants[0].stats["local"] is True


def test_spotify_id_of() -> None:
    assert spotify_id_of("spotify:track:abc") == "abc"
    assert spotify_id_of("spotify:local:x") is None and spotify_id_of(None) is None


def _t(i: int, title: str) -> dict[str, str]:
    return {"id": f"id{i}", "artist": "A", "title": title}


def test_approximate_tiers_from_top_lists() -> None:
    top = {
        "long_term": [_t(1, "Evergreen"), _t(2, "Old Love"), _t(1, "Evergreen")],
        "medium_term": [_t(3, "Summer Thing"), _t(1, "Evergreen"), _t(4, "Now Hit")],
        "short_term": [_t(4, "Now Hit")],
    }
    tiers = approximate_tiers(top)
    assert tiers.approximate is True
    assert [m.title for m in tiers.constants] == ["Evergreen"]  # long ∩ medium, deduped
    assert [m.title for m in tiers.flings] == ["Summer Thing"]  # medium - long - short
    assert tiers.constants[0].spotify_id == "id1" and tiers.constants[0].stats["approximate"]
    assert approximate_tiers({}).constants == []


def test_diff_members() -> None:
    entered, left = diff_members({"a", "b"}, {"b", "c"})
    assert entered == {"c"} and left == {"a"}
