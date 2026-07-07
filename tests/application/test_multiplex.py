"""Source multiplex layer: ALL (try every source) vs UNIFORM (round-robin)."""

from pathlib import Path

from crate.application.multiplex import MuxStrategy, SourceEntry, TierMux
from crate.domain.audio import AudioInfo
from crate.domain.download import DownloadOutcome
from crate.domain.identity import TrackIdentity
from crate.domain.tiers import SourceRef, Tier


def _flac() -> AudioInfo:
    return AudioInfo("flac", "flac", 48000, 2, 24, 1_800_000, 110.0, 25_000_000)


def _opus() -> AudioInfo:
    return AudioInfo("opus", "webm", 48000, 2, None, 130000, 110.0, 3_000_000)


def _src(name: str, tier: Tier, dl, lossless: bool = True):  # type: ignore[no-untyped-def]
    return SourceEntry(SourceRef(name, tier), dl, expected_lossless=lossless)


def _ok(source: str, tier: int):  # type: ignore[no-untyped-def]
    def dl(q: str, d: Path, w: TrackIdentity | None = None) -> DownloadOutcome:
        return DownloadOutcome(Path(f"/x/{source}.flac"), tier, source, 1.0, q)

    return dl


def test_all_tries_sources_in_order_first_verified_wins() -> None:
    calls: list[str] = []

    def s1(q: str, d: Path, w: TrackIdentity | None = None) -> DownloadOutcome:
        calls.append("s1")
        raise RuntimeError("miss")

    def s2(q: str, d: Path, w: TrackIdentity | None = None) -> DownloadOutcome:
        calls.append("s2")
        return DownloadOutcome(Path("/x/s2.flac"), 1, "s2", 1.0, q)

    mux = TierMux(Tier.ONE, [_src("s1", Tier.ONE, s1), _src("s2", Tier.ONE, s2)], MuxStrategy.ALL)
    report, attempts = mux.acquire("q", Path("/out"), prober=lambda p: _flac())

    assert report is not None and report.outcome.source == "s2"
    assert calls == ["s1", "s2"]  # tried s1, fell through to s2
    assert [(a.source, a.ok) for a in attempts] == [("s1", False), ("s2", True)]


def test_all_rejects_non_lossless_from_lossless_source() -> None:
    mux = TierMux(Tier.ONE, [_src("s1", Tier.ONE, _ok("s1", 1), lossless=True)], MuxStrategy.ALL)
    report, attempts = mux.acquire("q", Path("/out"), prober=lambda p: _opus())
    assert report is None
    assert attempts[0].ok is False and "rejected" in attempts[0].detail


def test_uniform_round_robins_primary_across_requests() -> None:
    sources = [
        _src("a", Tier.TWO, _ok("a", 2), lossless=False),
        _src("b", Tier.TWO, _ok("b", 2), lossless=False),
        _src("c", Tier.TWO, _ok("c", 2), lossless=False),
    ]
    mux = TierMux(Tier.TWO, sources, MuxStrategy.UNIFORM)
    winners = []
    for _ in range(4):
        report, _ = mux.acquire("q", Path("/out"), prober=lambda p: _flac())
        assert report is not None
        winners.append(report.outcome.source)
    # each request starts at the next source -> load spreads a,b,c,a,...
    assert winners == ["a", "b", "c", "a"]


def test_acquire_all_fail_returns_none() -> None:
    def boom(q: str, d: Path, w: TrackIdentity | None = None) -> DownloadOutcome:
        raise RuntimeError("down")

    mux = TierMux(Tier.ONE, [_src("s1", Tier.ONE, boom)], MuxStrategy.ALL)
    report, attempts = mux.acquire("q", Path("/out"), prober=lambda p: _flac())
    assert report is None and attempts[0].ok is False


def _flac_dur(seconds: float) -> AudioInfo:
    return AudioInfo("flac", "flac", 48000, 2, 24, 1_800_000, seconds, 25_000_000)


def test_acquire_passes_want_down_to_the_source() -> None:
    seen: list[TrackIdentity | None] = []

    def dl(q: str, d: Path, w: TrackIdentity | None = None) -> DownloadOutcome:
        seen.append(w)
        return DownloadOutcome(Path("/x/s.flac"), 1, "s", 1.0, q)

    want = TrackIdentity("A", "T", "US123", 110)
    mux = TierMux(Tier.ONE, [_src("s", Tier.ONE, dl)], MuxStrategy.ALL)
    mux.acquire("q", Path("/out"), prober=lambda p: _flac_dur(110), want=want)
    assert seen == [want]


def test_acquire_rejects_file_whose_duration_is_off() -> None:
    # wanted 110 s, file is 200 s -> post-probe mismatch, attempt fails, no winner
    want = TrackIdentity(title="T", duration_s=110)
    mux = TierMux(Tier.ONE, [_src("s", Tier.ONE, _ok("s", 1))], MuxStrategy.ALL)
    report, attempts = mux.acquire("q", Path("/out"), prober=lambda p: _flac_dur(200), want=want)
    assert report is None
    assert attempts[0].ok is False
    assert "duration mismatch" in attempts[0].detail
    assert "got 200s" in attempts[0].detail and "want 110s" in attempts[0].detail


def test_acquire_accepts_file_within_duration_tolerance() -> None:
    want = TrackIdentity(title="T", duration_s=110)
    mux = TierMux(Tier.ONE, [_src("s", Tier.ONE, _ok("s", 1))], MuxStrategy.ALL)
    # 116 s is off by 6, within the 7 s tolerance -> accepted
    report, _ = mux.acquire("q", Path("/out"), prober=lambda p: _flac_dur(116), want=want)
    assert report is not None and report.outcome.source == "s"
