"""slskd-compatible shim (ADR-0014): Explo drives these endpoints thinking it's
slskd; the shim runs the crate cascade instead and returns one synthetic result.

The cascade is injected as a ``download(query, out_dir) -> path`` callable, so the
endpoint logic + transfer state machine are tested without network."""

from pathlib import Path

from crate.adapters.inbound.slskd_shim import ShimState
from crate.domain.identity import TrackIdentity, from_query


def _dl_ok(flac_bytes: bytes = b"fLaC\x00shim"):  # type: ignore[no-untyped-def]
    def download(query: str, out_dir: Path) -> Path:
        p = out_dir / "result.flac"
        p.write_bytes(flac_bytes)
        return p

    return download


def test_search_returns_id_and_completes() -> None:
    shim = ShimState(download=_dl_ok(), downloads_dir=Path("/tmp"))
    started = shim.start_search("Daft Punk Get Lucky")
    sid = started["id"]
    assert started["searchText"] == "Daft Punk Get Lucky"
    state = shim.search_state(sid)
    assert state["state"].startswith("Completed")
    assert state["fileCount"] == 1


def test_search_responses_advertise_one_flac_from_user_crate() -> None:
    shim = ShimState(download=_dl_ok(), downloads_dir=Path("/tmp"))
    sid = shim.start_search("Diljit Dealer")["id"]
    resp = shim.search_responses(sid)
    assert len(resp) == 1
    r = resp[0]
    assert r["username"] == "crate"
    assert r["hasFreeUploadSlot"] is True
    f = r["files"][0]
    assert f["filename"].endswith(".flac")
    assert f["bitRate"] >= 256  # passes Explo's MIN_BITRATE


def test_enqueue_then_download_completes_and_writes_file(tmp_path: Path) -> None:
    shim = ShimState(download=_dl_ok(b"fLaC\x00realbytes"), downloads_dir=tmp_path)
    sid = shim.start_search("Diljit Dealer")["id"]
    fname = shim.search_responses(sid)[0]["files"][0]["filename"]

    shim.enqueue("crate", [{"filename": fname, "size": 123}])
    shim.run_download(fname)  # synchronous for the test (enqueue spawns a thread)

    transfers = shim.transfers("crate")
    files = transfers["directories"][0]["files"]
    assert files[0]["state"] == "Completed, Succeeded"
    # file landed at downloads_dir/<advertised filename> for Explo to pick up
    dest = tmp_path / fname
    assert dest.exists() and dest.read_bytes().startswith(b"fLaC")


def test_run_download_no_move_when_produced_at_dest(tmp_path: Path) -> None:
    # download writes directly to the advertised path -> no move needed
    def download(query: str, out_dir: Path) -> Path:
        p = out_dir / (query + ".flac")
        p.write_bytes(b"fLaC\x00x")
        return p

    shim = ShimState(download=download, downloads_dir=tmp_path)
    sid = shim.start_search("q")["id"]
    fname = shim.search_responses(sid)[0]["files"][0]["filename"]
    shim.run_download(fname)
    assert shim.transfers("crate")["directories"][0]["files"][0]["state"] == "Completed, Succeeded"


def test_download_failure_marks_errored(tmp_path: Path) -> None:
    def boom(query: str, out_dir: Path) -> Path:
        raise RuntimeError("all tiers failed")

    shim = ShimState(download=boom, downloads_dir=tmp_path)
    sid = shim.start_search("Nonexistent Track")["id"]
    fname = shim.search_responses(sid)[0]["files"][0]["filename"]
    shim.enqueue("crate", [{"filename": fname, "size": 1}])
    shim.run_download(fname)
    files = shim.transfers("crate")["directories"][0]["files"]
    assert "Errored" in files[0]["state"]


def test_download_receives_search_text_for_best_effort_identity(tmp_path: Path) -> None:
    # The shim advertises the search text and hands it to the download callable;
    # the composition closure parses a best-effort identity from it via from_query.
    seen: list[str] = []

    def download(query: str, out_dir: Path) -> Path:
        seen.append(query)
        p = out_dir / "out.flac"
        p.write_bytes(b"fLaC\x00x")
        return p

    shim = ShimState(download=download, downloads_dir=tmp_path)
    sid = shim.start_search("Daft Punk - Get Lucky")["id"]
    fname = shim.search_responses(sid)[0]["files"][0]["filename"]
    shim.run_download(fname)
    assert seen == ["Daft Punk - Get Lucky"]
    # the "Artist - Title" shape parses into a structured identity
    assert from_query(seen[0]) == TrackIdentity(artist="Daft Punk", title="Get Lucky")


def test_run_download_uses_filename_stem_when_search_text_unknown(tmp_path: Path) -> None:
    # If the transfer arrives without a prior search (no _file_query entry), the
    # query defaults to the filename stem — still a valid from_query input.
    seen: list[str] = []

    def download(query: str, out_dir: Path) -> Path:
        seen.append(query)
        p = out_dir / "out.flac"
        p.write_bytes(b"fLaC\x00x")
        return p

    shim = ShimState(download=download, downloads_dir=tmp_path)
    shim.run_download("Burial - Archangel.flac")
    assert seen == ["Burial - Archangel"]  # ".flac" stem
    assert from_query(seen[0]) == TrackIdentity(artist="Burial", title="Archangel")


def test_server_module_imports() -> None:
    # the stdlib server wiring is I/O (pragma), but it must import cleanly
    import crate.infrastructure.http_server.serve as srv

    assert hasattr(srv, "run_server")


def test_api_key_check() -> None:
    shim = ShimState(download=_dl_ok(), downloads_dir=Path("/tmp"), api_key="secret")
    assert shim.authorized({"X-API-Key": "secret"}) is True
    assert shim.authorized({"X-API-Key": "wrong"}) is False
    assert shim.authorized({}) is False
    # no key configured -> open
    assert ShimState(download=_dl_ok(), downloads_dir=Path("/tmp")).authorized({}) is True


def test_transfers_map_is_bounded_evicting_completed_first(tmp_path: Path) -> None:
    """Explo re-enqueues on every sync, so _transfers would grow unbounded. Once
    over the cap, oldest *finished* records are dropped; in-flight ones survive.
    250 completed + 5 in-flight -> map stays within the cap, in-flight preserved."""
    from crate.adapters.inbound.slskd_shim import _TRANSFERS_MAX

    shim = ShimState(download=_dl_ok(), downloads_dir=tmp_path)

    # 5 in-flight transfers that must never be evicted (Explo still polling them)
    inflight = [f"live-{i}.flac" for i in range(5)]
    for name in inflight:
        shim.enqueue("crate", [{"filename": name, "size": 1}])
        shim._transfers[name]["state"] = "InProgress"

    # 250 transfers that complete; each new enqueue re-checks the cap and evicts
    for i in range(250):
        name = f"done-{i}.flac"
        shim.enqueue("crate", [{"filename": name, "size": 1}])
        shim._transfers[name]["state"] = "Completed, Succeeded"

    assert len(shim._transfers) <= _TRANSFERS_MAX  # bounded despite 255 inserts
    files = {f["filename"] for f in shim.transfers("crate")["directories"][0]["files"]}
    assert set(inflight) <= files  # the 5 in-flight survived every eviction pass
