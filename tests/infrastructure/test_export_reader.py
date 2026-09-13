"""open_export: zip vs json sniffing, member selection, lazy per-file loads."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from crate.infrastructure.history.export_reader import open_export

_DIR = "Spotify Extended Streaming History"


def _zip(path: Path, members: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in members.items():
            zf.writestr(name, body)
    return path


def test_zip_yields_only_history_files_sorted(tmp_path: Path) -> None:
    upload = _zip(
        tmp_path / "x.upload",
        {
            f"{_DIR}/Streaming_History_Audio_2021_1.json": json.dumps([{"ts": "b"}]),
            f"{_DIR}/Streaming_History_Audio_2019-2020_0.json": json.dumps([{"ts": "a"}]),
            f"{_DIR}/Streaming_History_Video_2021.json": "[]",
            f"{_DIR}/ReadMeFirst_ExtendedStreamingHistory.pdf": "%PDF",
        },
    )
    files = open_export(upload)
    assert [f.name for f in files] == [
        "Streaming_History_Audio_2019-2020_0.json",
        "Streaming_History_Audio_2021_1.json",
    ]
    assert files[0].load() == [{"ts": "a"}]


def test_zip_accepts_the_short_account_data_export(tmp_path: Path) -> None:
    upload = _zip(
        tmp_path / "x.upload",
        {"MyData/StreamingHistory_music_0.json": "[]", "MyData/Userdata.json": "{}"},
    )
    assert [f.name for f in open_export(upload)] == ["StreamingHistory_music_0.json"]


def test_zip_without_history_is_rejected(tmp_path: Path) -> None:
    upload = _zip(tmp_path / "x.upload", {"MyData/Userdata.json": "{}"})
    with pytest.raises(ValueError, match="no Spotify streaming-history"):
        open_export(upload)


def test_single_json_file_and_bom(tmp_path: Path) -> None:
    upload = tmp_path / "x.upload"
    upload.write_bytes(b"\xef\xbb\xbf" + json.dumps([{"ts": "a"}, "junk"]).encode())
    [ref] = open_export(upload)
    assert ref.load() == [{"ts": "a"}]  # non-dict entries dropped


def test_invalid_json_and_wrong_shape_raise_with_the_file_name(tmp_path: Path) -> None:
    bad = tmp_path / "bad.upload"
    bad.write_bytes(b"[not json")
    with pytest.raises(ValueError, match=r"bad\.upload: not valid JSON"):
        open_export(bad)[0].load()
    obj = tmp_path / "obj.upload"
    obj.write_bytes(b'{"a": 1}')
    with pytest.raises(ValueError, match="expected a JSON list"):
        open_export(obj)[0].load()


def test_neither_zip_nor_json_is_rejected(tmp_path: Path) -> None:
    junk = tmp_path / "x.upload"
    junk.write_bytes(b"GIF89a....")
    with pytest.raises(ValueError, match="neither a zip nor a JSON"):
        open_export(junk)
