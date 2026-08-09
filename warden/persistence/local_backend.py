"""Local "mock S3" backend: tar.gz a task folder, atomically written to disk.

Mirrors the OpenHands ``LocalFileStore`` shape (string-keyed, atomic local
writes, streaming ``write_from_path``). The archive is built into a tempfile
**on disk** and ``os.replace``-d into place — we never buffer a multi-GB archive
in RAM, which is load-bearing per the reference implementation.

Archive build/extract logic lives in ``persistence.archive`` and is
shared with the S3 backend; this module only owns the local key→path mapping and
the atomic on-disk placement.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from warden.persistence.archive import (
    extract_tar_gz,
    read_member_from_tar,
    write_tar_gz,
)
from warden.persistence.backend import WorkspaceRestoreError


class LocalFileBackend:
    """A `StorageBackend` writing gzip tarballs under ``state_root``."""

    def __init__(
        self, state_root: Path, exclude_patterns: tuple[str, ...] = ()
    ) -> None:
        self._state_root = Path(state_root)
        self._exclude_patterns = tuple(exclude_patterns)

    # --- key → filesystem path ------------------------------------------------

    def _path_for(self, key: str) -> Path:
        return self._state_root / key

    # --- blocking implementations (run in a worker thread) --------------------

    def _backup_sync(self, local_dir: Path, key: str) -> dict:
        local_dir = Path(local_dir)
        dest = self._path_for(key)
        dest.parent.mkdir(parents=True, exist_ok=True)

        # Build into a tempfile in the destination dir (same filesystem → atomic
        # os.replace), so a partial/failed write never corrupts an existing key.
        fd, tmp_name = tempfile.mkstemp(
            dir=str(dest.parent), prefix=".tmp-", suffix=".tar.gz"
        )
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            stats = write_tar_gz(local_dir, tmp_path, self._exclude_patterns)
            os.replace(tmp_path, dest)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

        return {"key": key, "files": stats["files"], "bytes": stats["bytes"]}

    def _restore_sync(self, key: str, local_dir: Path) -> dict:
        src = self._path_for(key)
        if not src.exists():
            raise WorkspaceRestoreError(
                f"archive key not found in store: {key}",
                key=key,
                retries_attempted=0,
                last_error=None,
            )

        stats = extract_tar_gz(src, Path(local_dir), key)
        return {"key": key, "files": stats["files"], "bytes": stats["bytes"]}

    def _read_file_sync(self, key: str, member_path: str) -> bytes:
        src = self._path_for(key)
        if not src.exists():
            raise FileNotFoundError(f"archive key not found in store: {key}")
        return read_member_from_tar(src, member_path)

    # --- async surface --------------------------------------------------------

    async def backup(self, local_dir: Path, key: str) -> dict:
        return await asyncio.to_thread(self._backup_sync, local_dir, key)

    async def restore(self, key: str, local_dir: Path) -> dict:
        return await asyncio.to_thread(self._restore_sync, key, local_dir)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(lambda: self._path_for(key).exists())

    async def read_file(self, key: str, member_path: str) -> bytes:
        return await asyncio.to_thread(self._read_file_sync, key, member_path)
