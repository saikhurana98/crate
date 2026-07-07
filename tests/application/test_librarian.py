"""Librarian: per-track finalize — tag, organize, atomic move into the library."""

from pathlib import Path

from crate.application.download import DownloadReport
from crate.application.librarian import finalize, make_finalizer
from crate.domain.audio import AudioInfo, QualityVerdict
from crate.domain.download import DownloadOutcome
from crate.domain.library import TrackTags


def _staged(tmp: Path, name: str = "in.flac", data: bytes = b"AUDIO") -> Path:
    src = tmp / "staging" / name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(data)
    return src


def _reader(tags: TrackTags):  # type: ignore[no-untyped-def]
    return lambda _p: tags


def test_finalize_moves_into_artist_album_layout(tmp_path: Path) -> None:
    src = _staged(tmp_path)
    lib = tmp_path / "library"
    written: list[TrackTags] = []
    tags = TrackTags(artist="KK", album="Life", title="Yaaron", track_no=2)

    dest = finalize(src, lib, read_tags=_reader(tags), write_tags=lambda p, t: written.append(t))

    assert dest == lib / "KK" / "Life" / "02 Yaaron.flac"
    assert dest.read_bytes() == b"AUDIO"
    assert not src.exists()  # moved, not copied
    assert written and written[0].title == "Yaaron"  # tags written back (organize + tag)


def test_finalize_fills_missing_title_from_fallback(tmp_path: Path) -> None:
    src = _staged(tmp_path)
    lib = tmp_path / "library"
    dest = finalize(
        src,
        lib,
        read_tags=_reader(TrackTags(artist="X")),  # no title
        write_tags=lambda p, t: None,
        fallback_title="Pritam - Some Song",
    )
    assert dest == lib / "X" / "Unknown Album" / "Pritam - Some Song.flac"


def test_finalize_dedupes_a_different_existing_file(tmp_path: Path) -> None:
    lib = tmp_path / "library"
    tags = TrackTags(artist="A", album="B", title="C")
    # first track lands normally
    finalize(_staged(tmp_path, "1.flac", b"first"), lib, read_tags=_reader(tags), write_tags=_noop)
    # a different file with the same tags must not clobber the first
    dest2 = finalize(
        _staged(tmp_path, "2.flac", b"second-longer"),
        lib,
        read_tags=_reader(tags),
        write_tags=_noop,
    )
    assert dest2 == lib / "A" / "B" / "C (2).flac"
    assert (lib / "A" / "B" / "C.flac").read_bytes() == b"first"


def _noop(p: Path, t: TrackTags) -> None:
    return None


def _report(path: Path, query: str = "KK Yaaron", **meta: object) -> DownloadReport:
    outcome = DownloadOutcome(path, 1, "qobuz", 3.0, query, **meta)  # type: ignore[arg-type]
    info = AudioInfo("flac", "flac", 48000, 2, 24, 1_800_000, 110.0, 25_000_000)
    return DownloadReport(query, outcome, info, QualityVerdict("lossless", True), None)


def test_source_metadata_wins_over_file_tags(tmp_path: Path) -> None:
    src = _staged(tmp_path)
    lib = tmp_path / "library"
    written: list[TrackTags] = []
    # source says one thing, the (stale) file tags say another → source wins,
    # and the merged tags are written back onto the file.
    dest = finalize(
        src,
        lib,
        source=TrackTags(artist="Daft Punk", album="Discovery", title="Aerodynamic"),
        read_tags=_reader(TrackTags(artist="WRONG", album="WRONG", title="WRONG")),
        write_tags=lambda p, t: written.append(t),
    )
    assert dest == lib / "Daft Punk" / "Discovery" / "Aerodynamic.flac"
    assert written[0].artist == "Daft Punk" and written[0].album == "Discovery"


def test_file_tags_fill_gaps_the_source_left(tmp_path: Path) -> None:
    src = _staged(tmp_path)
    lib = tmp_path / "library"
    # source knew title only (e.g. Soulseek); the file carries artist/album/track#.
    dest = finalize(
        src,
        lib,
        source=TrackTags(title="Song"),
        read_tags=_reader(TrackTags(artist="A", album="B", track_no=5)),
        write_tags=_noop,
    )
    assert dest == lib / "A" / "B" / "05 Song.flac"


def test_make_finalizer_uses_outcome_metadata(tmp_path: Path) -> None:
    src = _staged(tmp_path)
    lib = tmp_path / "library"
    fin = make_finalizer(lib, read_tags=_reader(TrackTags()), write_tags=_noop)  # untagged file
    out = fin(_report(src, artist="Coldplay", album="Parachutes", title="Yellow", track_no=6))
    assert out.outcome is not None
    assert out.outcome.path == lib / "Coldplay" / "Parachutes" / "06 Yellow.flac"


def test_make_finalizer_rewrites_report_path(tmp_path: Path) -> None:
    src = _staged(tmp_path)
    lib = tmp_path / "library"
    fin = make_finalizer(
        lib, read_tags=_reader(TrackTags(title="Yaaron", artist="KK")), write_tags=_noop
    )

    out = fin(_report(src))

    assert out.outcome is not None
    assert out.outcome.path == lib / "KK" / "Unknown Album" / "Yaaron.flac"
    assert out.outcome.path.exists() and not src.exists()


def test_make_finalizer_passes_through_report_without_file(tmp_path: Path) -> None:
    fin = make_finalizer(tmp_path, read_tags=_reader(TrackTags()), write_tags=_noop)
    empty = DownloadReport("q", None, None, None, "no source produced a file")
    assert fin(empty) is empty
