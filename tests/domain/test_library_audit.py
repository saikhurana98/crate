"""Tag audit + album-folder duplicate rules (pure)."""

from __future__ import annotations

import pytest

from crate.domain.library_audit import folder_duplicates, folder_of, tags_disagree


@pytest.mark.parametrize(
    ("tags", "aid", "want"),
    [
        (("Diljit Dosanjh", "Lover"), ("Diljit Dosanjh", "Lover"), None),
        (("Diljit Dosanjh", "Lover (Official Video)"), ("Diljit Dosanjh", "Lover"), None),
        (("Daft Punk", "Around the World"), ("Daft Punk", "Get Lucky"), "title"),
        (("Alka Yagnik", "Alfaaz"), ("Yo Yo Honey Singh", "Alfaaz"), "artist"),
        (("Arijit Singh", "Tum Hi Ho"), ("Daft Punk", "Get Lucky"), "both"),
        ((None, None), ("Daft Punk", "Get Lucky"), "title"),  # an untagged artist isn't a clash
        (("Arijit Singh", "Tum Hi Ho"), (None, None), None),
    ],
)
def test_tags_disagree(
    tags: tuple[str | None, str | None], aid: tuple[str | None, str | None], want: str | None
) -> None:
    assert tags_disagree(tags[0], tags[1], aid[0], aid[1]) == want


def test_folder_of() -> None:
    assert folder_of("A/LP/01 x.flac") == "A/LP" and folder_of("x.flac") == ""


def test_an_album_filed_twice_is_reported_once() -> None:
    members = [(f"g{i}", "A/LP") for i in range(4)] + [(f"g{i}", "A/LP (Deluxe)") for i in range(4)]
    members += [("g9", "A/LP"), ("g9", "Misc")]  # one stray duplicate elsewhere
    sizes = {"A/LP": 4, "A/LP (Deluxe)": 6, "Misc": 40}
    dups = folder_duplicates(members, sizes)
    assert len(dups) == 1
    d = dups[0]
    assert (d.folder, d.other, d.shared, d.files, d.other_files, d.ratio) == (
        "A/LP",
        "A/LP (Deluxe)",
        4,
        4,
        6,
        1.0,
    )


def test_low_overlap_or_tiny_overlap_is_not_an_album_copy() -> None:
    members = [("g1", "A"), ("g1", "B"), ("g2", "A"), ("g2", "B")]
    assert folder_duplicates(members, {"A": 10, "B": 10}) == []  # 20% overlap
    assert folder_duplicates([("g1", "A"), ("g1", "B")], {"A": 1, "B": 1}) == []  # min_shared
    assert folder_duplicates(members, {"A": 2, "B": 3}, threshold=0.7)[0].folder == "A"
