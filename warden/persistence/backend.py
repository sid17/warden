"""Storage backend contract (swappable).

`StorageBackend` is the one interface every store implements. `LocalFileBackend`
satisfies it now; a future `S3Boto3Backend` will satisfy it unchanged. Callers
depend only on this Protocol.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


class WorkspaceRestoreError(Exception):
    """Raised when a restore cannot complete (e.g. key missing, transport fail).

    Attributes:
        key: The archive key that failed to restore.
        retries_attempted: How many restore attempts were made.
        last_error: The underlying exception, if any.
    """

    def __init__(
        self,
        message: str,
        *,
        key: str,
        retries_attempted: int = 0,
        last_error: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.key = key
        self.retries_attempted = retries_attempted
        self.last_error = last_error


@runtime_checkable
class StorageBackend(Protocol):
    """Backup/restore/exists over a string-keyed archive store."""

    async def backup(self, local_dir: Path, key: str) -> dict:
        """Archive ``local_dir`` into the store at ``key``. Returns stats dict."""
        ...

    async def restore(self, key: str, local_dir: Path) -> dict:
        """Extract the archive at ``key`` into ``local_dir``. Returns stats dict.

        Raises:
            WorkspaceRestoreError: if ``key`` does not exist in the store.
        """
        ...

    async def exists(self, key: str) -> bool:
        """Return whether ``key`` exists in the store."""
        ...

    async def read_file(self, key: str, member_path: str) -> bytes:
        """Return the bytes of ONE confined member from the archive at ``key``.

        The read-side counterpart to the whole-folder ``restore`` (EXT-A1): serve a
        single generated file from the persistence snapshot (authoritative after
        teardown) without restoring the whole workspace. ``member_path`` is validated
        segment-wise (``..``/NUL/absolute/escaping-link rejected) so a caller can
        never read outside the archive. Credential files are already excluded from
        every snapshot, so a secret can never be served.

        Raises:
            ValueError: ``member_path`` fails the confinement check.
            FileNotFoundError: the archive key or the member does not exist.
        """
        ...
