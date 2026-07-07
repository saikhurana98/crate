"""Tier-3 source adapter: yt-dlp best-audio download (ADR-0015 Tier 3).

Lowest tier — lossy fallback (YouTube best audio, typically Opus ~160 kbps or
AAC). Emits an instant playable file when the FLAC tiers cannot deliver.

Accuracy: a bare ``ytsearch1:`` takes the *first* YouTube hit with zero
verification, which routinely files a "Full Video Song | Movie | Stars" webm
under the wrong artist. Instead we fetch the top few candidates, drop the ones
whose title doesn't match the query (and, when known, whose duration is off),
then pick the highest-scoring one via :mod:`crate.domain.matching`. If that
search step fails for any reason we fall back to the old single-shot
``ytsearch1:`` download so Tier 3 never goes dark.

The subprocess is injected (``runner``) and the clock is injected so the
command-building, candidate parsing, and selection logic are unit tested
without network. Every subprocess call is bounded by a timeout: a wedged yt-dlp
surfaces as ``RuntimeError`` instead of pinning the worker thread forever.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from crate.domain.download import DownloadOutcome
from crate.domain.matching import match_score, title_matches

__all__ = [
    "DownloadOutcome",
    "YtCandidate",
    "build_command",
    "build_search_command",
    "download",
    "extract_path",
    "parse_candidates",
    "select_candidate",
]

# Runner takes (argv, timeout_s); the default passes the timeout to
# subprocess.run so a hung yt-dlp is killed. TimeoutExpired is translated to
# RuntimeError by the callers, not swallowed here.
Runner = Callable[[list[str], float], "subprocess.CompletedProcess[str]"]
Clock = Callable[[], float]

_SEARCH_TIMEOUT_S = 60.0
_DOWNLOAD_TIMEOUT_S = 600.0
# How many candidates to inspect before selecting.
_SEARCH_N = 5
# Accept a candidate only if its runtime is within this of the expected one.
_DURATION_TOLERANCE_S = 7
# Tab-separated --print fields: id, title, channel, duration.
_CANDIDATE_FIELDS = 4


@dataclass(frozen=True, slots=True)
class YtCandidate:
    id: str
    title: str
    channel: str
    duration_s: int | None


def build_search_command(query: str, n: int = _SEARCH_N) -> list[str]:
    """Build the metadata-only ``ytsearchN:`` probe (no download)."""
    return [
        "yt-dlp",
        f"ytsearch{n}:{query}",
        "--skip-download",
        "--no-warnings",
        "--print",
        "%(id)s\t%(title)s\t%(channel)s\t%(duration)s",
    ]


def parse_candidates(stdout: str) -> list[YtCandidate]:
    """Parse the tab-separated ``--print`` rows; malformed lines are skipped."""
    cands: list[YtCandidate] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < _CANDIDATE_FIELDS:
            continue
        vid, title, channel, dur = parts[0], parts[1], parts[2], parts[3]
        if not vid:
            continue
        try:
            duration: int | None = int(float(dur))
        except (TypeError, ValueError):
            duration = None
        cands.append(YtCandidate(id=vid, title=title, channel=channel, duration_s=duration))
    return cands


def select_candidate(
    cands: list[YtCandidate],
    query: str,
    expected_duration_s: int | None = None,
) -> YtCandidate:
    """Pick the best title-matching candidate.

    Drops candidates whose title doesn't match the query; when
    ``expected_duration_s`` is given, drops those off by more than the tolerance
    (but keeps all if that would empty the pool — a wrong duration beats no
    result). Ranks the survivors by :func:`match_score` (title + channel overlap
    with the query) and returns the top one. Raises ``LookupError`` when nothing
    title-matches.
    """
    matching = [c for c in cands if title_matches(c.title, query)]
    if not matching:
        raise LookupError(f"no title-matching yt-dlp candidate for {query!r}")

    pool = matching
    if expected_duration_s is not None:
        near = [
            c
            for c in matching
            if c.duration_s is not None
            and abs(c.duration_s - expected_duration_s) <= _DURATION_TOLERANCE_S
        ]
        if near:  # keep all title-matches if the duration filter empties the pool
            pool = near

    return max(pool, key=lambda c: match_score(c.title, c.channel, query))


def build_command(query: str, out_dir: Path) -> list[str]:
    """Build the single-shot yt-dlp download argv. Free text becomes a
    ``ytsearch1:`` query; a URL is passed through unchanged. Used by the direct
    download path and as the search-failure fallback."""
    target = query if query.startswith(("http://", "https://")) else f"ytsearch1:{query}"
    return [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "-f",
        "bestaudio",
        "-o",
        f"{out_dir}/%(title)s [%(id)s].%(ext)s",
        "--print",
        "after_move:filepath",
        target,
    ]


def extract_path(stdout: str) -> Path:
    """The ``--print after_move:filepath`` output is the resolved file path."""
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError("yt-dlp produced no output path")
    return Path(lines[-1])


def _default_runner(  # pragma: no cover - real subprocess
    cmd: list[str], timeout_s: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout_s)


def _run(runner: Runner, cmd: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
    """Invoke the runner, translating a subprocess timeout to RuntimeError."""
    try:
        return runner(cmd, timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"yt-dlp timed out after {timeout_s:.0f}s") from exc


def _outcome(path: Path, query: str, elapsed: float, title: str | None) -> DownloadOutcome:
    # title comes from the chosen candidate; artist stays None — a YouTube
    # channel is not an artist and would mis-tag the file.
    return DownloadOutcome(
        path=path,
        tier=3,
        source="ytdlp",
        elapsed_s=elapsed,
        query=query,
        title=title,
    )


def _fallback_download(
    query: str, out_dir: Path, *, runner: Runner, clock: Clock, start: float
) -> DownloadOutcome:
    """Old single-shot ``ytsearch1:`` path — keeps Tier 3 alive when the scored
    search fails (nonzero exit or unparseable candidates)."""
    cmd = build_command(query, out_dir)
    result = _run(runner, cmd, _DOWNLOAD_TIMEOUT_S)
    elapsed = clock() - start
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp failed ({result.returncode}): {result.stderr.strip()}")
    return _outcome(extract_path(result.stdout), query, elapsed, title=None)


def download(
    query: str,
    out_dir: Path,
    *,
    runner: Runner = _default_runner,
    clock: Clock = time.monotonic,
    expected_duration_s: int | None = None,
) -> DownloadOutcome:
    """Search, verify, and download the best-audio rendition of ``query``.

    Runs a metadata-only ``ytsearch5:`` probe, selects the best title-matching
    candidate, then downloads it by id. If the *search* step fails (nonzero exit
    or no parseable candidates), falls back to the single-shot ``ytsearch1:``
    download so Tier 3 never goes dark. A URL query skips search entirely.
    """
    start = clock()

    if query.startswith(("http://", "https://")):
        return _fallback_download(query, out_dir, runner=runner, clock=clock, start=start)

    search = _run(runner, build_search_command(query), _SEARCH_TIMEOUT_S)
    if search.returncode != 0:
        return _fallback_download(query, out_dir, runner=runner, clock=clock, start=start)
    candidates = parse_candidates(search.stdout)
    if not candidates:
        return _fallback_download(query, out_dir, runner=runner, clock=clock, start=start)

    chosen = select_candidate(candidates, query, expected_duration_s)

    dl_cmd = build_command(f"https://www.youtube.com/watch?v={chosen.id}", out_dir)
    result = _run(runner, dl_cmd, _DOWNLOAD_TIMEOUT_S)
    elapsed = clock() - start
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp failed ({result.returncode}): {result.stderr.strip()}")
    return _outcome(extract_path(result.stdout), query, elapsed, title=chosen.title)
