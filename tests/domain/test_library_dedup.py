"""Duplicate components and keeper ranking (pure)."""

from __future__ import annotations

from dataclasses import replace

from crate.domain.library_dedup import (
    ACOUSTID,
    EXACT,
    FP,
    Edge,
    FileFacts,
    connected_groups,
    is_variant,
    pair,
    rank_files,
)


def facts(fid: str, **kw: object) -> FileFacts:
    base = FileFacts(
        id=fid,
        path=f"{fid}.flac",
        lossless=True,
        bit_depth=16,
        sample_rate=44100,
        bitrate=None,
        duration_s=200.0,
        tag_score=5,
        has_cover=True,
        canonical=True,
        referenced=False,
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def test_edge_and_pair_are_ordered() -> None:
    assert Edge.of("b", "a", FP, 0.9) == Edge("a", "b", FP, 0.9)
    assert pair("z", "c") == ("c", "z")


def test_components_take_the_strongest_reason_and_honour_ignores() -> None:
    edges = [
        Edge.of("a", "b", EXACT, 1.0),
        Edge.of("b", "c", FP, 0.95),
        Edge.of("d", "e", ACOUSTID, 0.97),
        Edge.of("d", "e", FP, 0.93),
        Edge.of("x", "y", FP, 0.94),
        Edge.of("y", "z", FP, 0.96),
    ]
    comps = connected_groups(edges, ignored={("d", "e")})
    assert [(c.members, c.reason, c.confidence) for c in comps] == [
        (("a", "b", "c"), EXACT, 1.0),
        (("x", "y", "z"), FP, 0.94),
    ]
    again = connected_groups(list(reversed(edges)), ignored={("d", "e")})
    assert [c.signature for c in again] == [c.signature for c in comps]  # stable ids


def test_is_variant() -> None:
    assert is_variant([200.0, 215.0]) and not is_variant([200.0, 209.0]) and not is_variant([200.0])


def test_ranking_order_and_reasons() -> None:
    ranked = rank_files(
        [
            facts("mp3", lossless=False, bit_depth=None, bitrate=320_000),
            facts("cd", bit_depth=16),
            facts("hires", bit_depth=24, sample_rate=96000),
            facts("mp3low", lossless=False, bit_depth=None, bitrate=128_000),
        ]
    )
    assert [r.facts.id for r in ranked] == ["hires", "cd", "mp3", "mp3low"]
    assert ranked[0].reasons == []
    assert ranked[1].reasons == ["24-bit beats 16-bit"]
    assert ranked[2].reasons == ["lossless beats lossy"]


def test_lossy_copies_rank_by_bitrate_and_sample_rate() -> None:
    ranked = rank_files(
        [
            facts("a", lossless=False, bit_depth=None, bitrate=128_000),
            facts("b", lossless=False, bit_depth=None, bitrate=320_000),
            facts("c", lossless=False, bit_depth=None, bitrate=320_000, sample_rate=22050),
        ]
    )
    assert [r.facts.id for r in ranked] == ["b", "a", "c"]
    assert ranked[1].reasons == ["320 kbps beats 128 kbps"]
    assert ranked[2].reasons == ["44.1 kHz beats 22.05 kHz"]


def test_later_checks_break_ties_in_order() -> None:
    cases = [
        (facts("x", duration_s=260.0), "its length matches the rest of the group"),
        (facts("x", tag_score=2), "more complete tags"),
        (facts("x", has_cover=False), "has embedded cover art"),
        (facts("x", canonical=False), "sits at its canonical library path"),
    ]
    for loser, reason in cases:
        ranked = rank_files([loser, facts("keep"), facts("third")])
        assert ranked[0].facts.id == "keep"
        assert next(r for r in ranked if r.facts.id == "x").reasons == [reason]
    ranked = rank_files([facts("x"), facts("keep", referenced=True)])
    assert ranked[1].reasons == ["a download record points at it"]


def test_a_full_tie_keeps_the_shorter_path() -> None:
    ranked = rank_files([facts("long", path="a/b/c/song.flac"), facts("short", path="a/song.flac")])
    assert ranked[0].facts.id == "short"
    assert ranked[1].reasons == ["same on every check; kept the shorter path"]
