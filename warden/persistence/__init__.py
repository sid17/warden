"""Task persistence storage core (L3) — the durability mechanism.

Exports the storage primitives: config, key derivation, the backend contract,
and the concrete local/S3 backends. The *workspace* concerns that used to be
re-exported here — bootstrap scaffolding and the ``home_env`` / ``ensure_restored``
/ ``snapshot`` glue — now live in ``warden.workspace`` (L0), which
depends on this package (never the reverse).
"""

from warden.persistence.backend import (
    StorageBackend,
    WorkspaceRestoreError,
)
from warden.persistence.config import PersistenceConfig
from warden.persistence.factory import get_backend
from warden.persistence.keys import archive_key, task_dir, workspace_key
from warden.persistence.local_backend import LocalFileBackend
from warden.persistence.s3_backend import S3Boto3Backend

__all__ = [
    "PersistenceConfig",
    "workspace_key",
    "task_dir",
    "archive_key",
    "StorageBackend",
    "WorkspaceRestoreError",
    "LocalFileBackend",
    "S3Boto3Backend",
    "get_backend",
]
