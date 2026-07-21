"""Tier-3 yt-dlp adapter. Command-building, candidate parsing, and scored
selection are tested with an injected fake runner/clock; no network."""

import subprocess
from pathlib import Path

import pytest

from crate.adapters.sources.ytdlp import (
    YtCandidate,
    build_command,
    build_search_command,
    build_stream_url_command,
    download,
    extract_path,
    parse_candidates,
    parse_stream_url,
    search_candidates,
    select_candidate,
)


def _cp(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# command building
# ---------------------------------------------------------------------------


def test_build_stream_url_command_builds_watch_url_from_valid_id() -> None:
    cmd = build_stream_url_command("dQw4w9WgXcQ")
    assert cmd[0] == "yt-dlp" and "-g" in cmd
    assert cmd[-1] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_build_stream_url_command_rejects_non_id_refs() -> None:
    # SSRF/injection guard: only the exact 11-char id grammar is accepted, so a
    # ref can never smuggle a full URL or shell meta into yt-dlp.
    for bad in ("https://evil.example/x", "abc; rm -rf", "short", "toolongvideoid00"):
        with pytest.raises(ValueError, match="invalid youtube video id"):
            build_stream_url_command(bad)


def test_parse_stream_url_takes_first_http_line() -> None:
    assert (
        parse_stream_url("WARNING: x\nhttps://cdn.example/a.m4a\n") == "https://cdn.example/a.m4a"
    )
    with pytest.raises(RuntimeError, match="no stream url"):
        parse_stream_url("WARNING: nothing here\n")


def test_build_command_uses_ytsearch_for_free_text() -> None:
    cmd = build_command("Dua Lipa Levitating", Path("/tmp/out"))
    assert cmd[0] == "yt-dlp"
    assert "bestaudio" in cmd
    assert cmd[-1] == "ytsearch1:Dua Lipa Levitating"


def test_build_command_passes_url_through() -> None:
    url = "https://youtube.com/watch?v=abc"
    cmd = build_command(url, Path("/tmp/out"))
    assert cmd[-1] == url


def test_build_search_command_is_metadata_only() -> None:
    cmd = build_search_command("Karan Aujla Softly", n=5)
    assert cmd[0] == "yt-dlp"
    assert cmd[1] == "ytsearch5:Karan Aujla Softly"
    assert "--skip-download" in cmd
    assert "%(id)s\t%(title)s\t%(channel)s\t%(duration)s" in cmd


def test_extract_path_takes_last_nonempty_line() -> None:
    stdout = "some log\n\n/tmp/out/Song [id].opus\n"
    assert extract_path(stdout) == Path("/tmp/out/Song [id].opus")


def test_extract_path_raises_on_empty() -> None:
    with pytest.raises(RuntimeError, match="no output path"):
        extract_path("   \n\n")


# ---------------------------------------------------------------------------
# candidate parsing
# ---------------------------------------------------------------------------


def test_parse_candidates_tolerates_malformed_lines() -> None:
    stdout = (
        "id1\tSong One\tArtist One\t210\n"
        "\n"
        "garbage-line-without-tabs\n"
        "id2\tSong Two\tArtist Two\tNA\n"  # unparseable duration -> None
        "\tOnly title\tchan\t100\n"  # empty id -> dropped
        "id3\tSong Three\tArtist Three\t180.9\n"  # float duration -> int
    )
    cands = parse_candidates(stdout)
    assert [c.id for c in cands] == ["id1", "id2", "id3"]
    assert cands[0].duration_s == 210
    assert cands[1].duration_s is None
    assert cands[2].duration_s == 180


def test_parse_candidates_empty_stdout() -> None:
    assert parse_candidates("") == []


# ---------------------------------------------------------------------------
# scored selection
# ---------------------------------------------------------------------------


def test_select_skips_junk_first_hit_via_match_score() -> None:
    # The first hit is a mislabelled full-movie video; the second is the real
    # "Artist - Song". match_score (title+channel overlap with the query) must
    # lift the correct one above the junk first hit.
    cands = [
        YtCandidate("j1", "Aadat Se Majboor Full Video Song | Movie | Stars", "T-Series", 240),
        YtCandidate("c1", "Karan Aujla - Softly", "Karan Aujla", 200),
    ]
    chosen = select_candidate(cands, "Karan Aujla Softly")
    assert chosen.id == "c1"


def test_select_drops_off_duration_candidates() -> None:
    cands = [
        YtCandidate("far", "Karan Aujla Softly", "Karan Aujla", 400),  # off by 200s
        YtCandidate("near", "Karan Aujla Softly Slowed", "Karan Aujla", 203),  # within 7s
    ]
    chosen = select_candidate(cands, "Karan Aujla Softly", expected_duration_s=200)
    assert chosen.id == "near"


def test_select_keeps_all_when_duration_filter_empties_pool() -> None:
    # No candidate is within tolerance -> fall back to the title-matched pool
    # rather than returning nothing.
    cands = [
        YtCandidate("a", "Karan Aujla Softly", "Karan Aujla", 400),
        YtCandidate("b", "Karan Aujla Softly", "Some Reupload", None),
    ]
    chosen = select_candidate(cands, "Karan Aujla Softly", expected_duration_s=200)
    assert chosen.id in {"a", "b"}


def test_select_channel_gate_drops_zero_overlap_channel_when_real_one_exists() -> None:
    # Lenient artist gate: an obviously-unrelated channel is shed only because a
    # channel that shares an artist token is also available.
    cands = [
        YtCandidate("wrong", "Alfaaz Hai Mera Dil", "RandomAggregator", 210),
        YtCandidate("right", "Alfaaz Hai Mera Dil", "Alfaaz - Topic", 210),
    ]
    chosen = select_candidate(cands, "Alfaaz Hai Mera Dil", want_artist="Alfaaz")
    assert chosen.id == "right"


def test_select_channel_gate_keeps_pool_when_gate_would_empty_it() -> None:
    # A YouTube channel is a label/aggregator, not an artist: if NO channel shares
    # a token with the wanted artist, keep every title-match (don't kill Tier 3).
    cands = [YtCandidate("only", "Alfaaz Hai Mera Dil", "SomeLabelVEVO", 210)]
    chosen = select_candidate(cands, "Alfaaz Hai Mera Dil", want_artist="Alfaaz")
    assert chosen.id == "only"


def test_select_raises_lookuperror_when_nothing_title_matches() -> None:
    cands = [YtCandidate("x", "Totally Unrelated Track", "Nobody", 200)]
    with pytest.raises(LookupError, match="no title-matching"):
        select_candidate(cands, "Karan Aujla Softly")


def test_select_raises_lookuperror_on_empty_pool() -> None:
    with pytest.raises(LookupError, match="no title-matching"):
        select_candidate([], "Karan Aujla Softly")


# ---------------------------------------------------------------------------
# download orchestration
# ---------------------------------------------------------------------------


def test_download_searches_selects_then_downloads_by_id() -> None:
    calls: list[tuple[list[str], float]] = []
    search_out = (
        "j1\tAadat Se Majboor Full Video Song | Movie | Stars\tT-Series\t240\n"
        "c1\tKaran Aujla - Softly\tKaran Aujla\t200\n"
    )

    def fake_runner(cmd: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
        calls.append((cmd, timeout_s))
        if "--skip-download" in cmd:
            return _cp(0, stdout=search_out)
        return _cp(0, stdout="/tmp/out/Karan Aujla - Softly [c1].opus\n")

    times = iter([100.0, 104.0])
    outcome = download(
        "Karan Aujla Softly",
        Path("/tmp/out"),
        runner=fake_runner,
        clock=lambda: next(times),
    )
    assert outcome.path == Path("/tmp/out/Karan Aujla - Softly [c1].opus")
    assert outcome.tier == 3
    assert outcome.source == "ytdlp"
    assert outcome.elapsed_s == pytest.approx(4.0)
    # title from the chosen candidate; artist stays None (channel != artist)
    assert outcome.title == "Karan Aujla - Softly"
    assert outcome.artist is None
    # search ran with the 60s budget, the by-id download with the 600s budget
    assert calls[0][1] == pytest.approx(60.0)
    assert "--skip-download" in calls[0][0]
    assert calls[1][1] == pytest.approx(600.0)
    assert "https://www.youtube.com/watch?v=c1" in calls[1][0]


def test_download_url_query_skips_search() -> None:
    calls: list[list[str]] = []

    def fake_runner(cmd: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _cp(0, stdout="/tmp/out/Track [x].opus\n")

    outcome = download(
        "https://youtube.com/watch?v=x",
        Path("/tmp/out"),
        runner=fake_runner,
        clock=iter([0.0, 2.5]).__next__,
    )
    assert outcome.path == Path("/tmp/out/Track [x].opus")
    assert outcome.title is None
    # single call, no search step
    assert len(calls) == 1
    assert not any("--skip-download" in c for c in calls)


def test_download_falls_back_when_search_exits_nonzero() -> None:
    calls: list[list[str]] = []

    def fake_runner(cmd: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if "--skip-download" in cmd:
            return _cp(1, stderr="search boom")
        return _cp(0, stdout="/tmp/out/Fallback [f].opus\n")

    outcome = download(
        "Some Query",
        Path("/tmp/out"),
        runner=fake_runner,
        clock=iter([0.0, 3.0]).__next__,
    )
    assert outcome.path == Path("/tmp/out/Fallback [f].opus")
    # the fallback uses the single-shot ytsearch1 command
    assert any("ytsearch1:Some Query" in c for c in calls[-1])


def test_download_falls_back_when_search_unparseable() -> None:
    calls: list[list[str]] = []

    def fake_runner(cmd: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if "--skip-download" in cmd:
            return _cp(0, stdout="no tabs here at all\n")  # yields zero candidates
        return _cp(0, stdout="/tmp/out/Fallback [f].opus\n")

    outcome = download("Some Query", Path("/tmp/out"), runner=fake_runner, clock=lambda: 0.0)
    assert outcome.path == Path("/tmp/out/Fallback [f].opus")
    assert any("ytsearch1:Some Query" in c for c in calls[-1])


def test_download_raises_on_download_nonzero_exit() -> None:
    def fake_runner(cmd: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
        if "--skip-download" in cmd:
            return _cp(0, stdout="c1\tKaran Aujla - Softly\tKaran Aujla\t200\n")
        return _cp(1, stderr="download boom")

    with pytest.raises(RuntimeError, match="yt-dlp failed"):
        download("Karan Aujla Softly", Path("/tmp/out"), runner=fake_runner, clock=lambda: 0.0)


def test_download_raises_on_fallback_nonzero_exit() -> None:
    def fake_runner(cmd: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
        return _cp(1, stderr="boom")  # both search and fallback fail

    with pytest.raises(RuntimeError, match="yt-dlp failed"):
        download("nope", Path("/tmp/out"), runner=fake_runner, clock=lambda: 0.0)


def test_download_raises_runtimeerror_on_timeout() -> None:
    def timing_out_runner(cmd: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, timeout_s)

    with pytest.raises(RuntimeError, match="yt-dlp timed out"):
        download("anything", Path("/tmp/out"), runner=timing_out_runner, clock=lambda: 0.0)


def test_search_candidates_salvages_results_on_nonzero_exit() -> None:
    # yt-dlp exits nonzero when one ytsearch hit is unavailable, but still printed
    # the good rows to stdout — they must not be discarded.
    stdout = "c1\tDaft Punk - Get Lucky\tDaft Punk\t248\n"

    def runner(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        return _cp(1, stdout=stdout, stderr="ERROR: [youtube] X: This video is not available")

    cands = search_candidates("Daft Punk Get Lucky", runner=runner)
    assert [c.ref for c in cands] == ["c1"]
    assert cands[0].title == "Daft Punk - Get Lucky"


def test_download_uses_search_results_despite_nonzero_search_exit() -> None:
    search_out = "c1\tDaft Punk - Get Lucky\tDaft Punk\t248\n"
    seen: list[list[str]] = []

    def runner(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        seen.append(cmd)
        if "--skip-download" in cmd:  # the ytsearch5 probe fails but prints results
            return _cp(1, stdout=search_out, stderr="ERROR: one hit unavailable")
        return _cp(0, stdout="/tmp/out/Daft Punk - Get Lucky [c1].opus\n")

    times = iter([100.0, 104.0])
    outcome = download(
        "Daft Punk Get Lucky", Path("/tmp/out"), runner=runner, clock=lambda: next(times)
    )
    assert (
        outcome.title == "Daft Punk - Get Lucky"
    )  # salvaged candidate, not the ytsearch1 fallback
    assert any("watch?v=c1" in arg for cmd in seen for arg in cmd)  # downloaded by id
