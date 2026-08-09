"""Backend factory: construct a ``StorageBackend`` from a ``kind`` string.

``get_backend("local", ...)`` and ``get_backend("s3", ...)`` are the two
supported stores; the S3 backend is imported lazily inside the branch so the
``local`` path never pulls in boto3-adjacent modules.
"""

from __future__ import annotations

from warden.persistence.backend import StorageBackend
from warden.persistence.local_backend import LocalFileBackend


def get_backend(kind: str, **kwargs) -> StorageBackend:
    """Return a storage backend for ``kind`` (``"local"`` or ``"s3"``).

    Raises:
        ValueError: if ``kind`` is not a recognized backend.
    """
    if kind == "local":
        return LocalFileBackend(**kwargs)
    if kind == "s3":
        from warden.persistence.s3_backend import S3Boto3Backend

        return S3Boto3Backend(**kwargs)
    raise ValueError(f"unknown storage backend kind: {kind!r}")
