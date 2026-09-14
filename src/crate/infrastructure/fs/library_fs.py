"""The library filesystem as purge sees it: stat, same-filesystem rename, delete.

Every path is relative to the root and must stay inside it. A move is always an
``os.rename``: it's atomic, and across devices it raises ``EXDEV`` instead of
silently copying. Moving a file out of a folder removes folders left empty
(never the root, never a folder that still holds cover art).
"""

from __future__ import annotations

import os
from pathlib import Path

from crate.domain.purge import TRASH_DIR


class LibraryFs:
    def __init__(self, root: Path) -> None:
        self._root = Path(os.path.normpath(root))

    def _path(self, rel: str) -> Path:
        path = Path(os.path.normpath(self._root / rel))
        if path == self._root or not path.is_relative_to(self._root):
            raise ValueError(f"{rel!r} is outside the library")
        return path

    def stat(self, rel: str) -> tuple[int, int] | None:
        try:
            st = os.stat(self._path(rel), follow_symlinks=False)
        except FileNotFoundError:
            return None
        return st.st_size, st.st_mtime_ns

    def exists(self, rel: str) -> bool:
        return os.path.lexists(self._path(rel))

    def move(self, src: str, dst: str) -> None:
        source, dest = self._path(src), self._path(dst)
        if os.path.lexists(dest):
            raise FileExistsError(f"{dst} already exists")
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.rename(source, dest)
        self._prune_empty(source.parent)

    def unlink(self, rel: str) -> None:
        path = self._path(rel)
        path.unlink(missing_ok=True)
        self._prune_empty(path.parent)

    def ensure_trash_root(self) -> None:
        trash = self._root / TRASH_DIR
        trash.mkdir(exist_ok=True)
        (trash / ".ndignore").touch()  # Navidrome: don't index the trash

    def _prune_empty(self, folder: Path) -> None:
        stop = {self._root, self._root / TRASH_DIR}
        while folder not in stop and folder.is_relative_to(self._root):
            try:
                folder.rmdir()  # only succeeds when empty
            except OSError:
                return
            folder = folder.parent
