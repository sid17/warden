"""The single place path/key patterns live.

Same key string drives both the local storage root and (future) the S3 prefix,
so there is exactly one derivation to keep in sync. Human-friendly ids
("default", "task_1") pass through unchanged; only filesystem-unsafe characters
(path separators, traversal, leading dots) are rejected/sanitized.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid import cycle at runtime; only needed for typing
    from warden.persistence.config import PersistenceConfig


class InvalidWorkspaceId(ValueError):
    """Raised when a user_id/task_id is unsafe as a path segment."""


def _sanitize_id(raw: str, *, field: str) -> str:
    """Validate a single path segment.

    Rejects empty, path separators, parent-traversal, and leading dots (hidden
    dirs / dotfiles). UUIDs are stored dash-free upstream but that is the
    caller's choice — here we only guarantee filesystem safety.
    """
    if not raw or not raw.strip():
        raise InvalidWorkspaceId(f"{field} must be non-empty")
    if "/" in raw or "\\" in raw:
        raise InvalidWorkspaceId(f"{field} must not contain path separators: {raw!r}")
    if ".." in raw:
        raise InvalidWorkspaceId(f"{field} must not contain '..': {raw!r}")
    if raw.startswith("."):
        raise InvalidWorkspaceId(f"{field} must not start with '.': {raw!r}")
    return raw


def workspace_key(user_id: str, task_id: str, prefix: str = "v1") -> str:
    """Return the identity key: ``{prefix}/{user_id}/{task_id}``.

    Same string is used for the local storage root and the future S3 prefix.
    """
    user = _sanitize_id(user_id, field="user_id")
    task = _sanitize_id(task_id, field="task_id")
    return f"{prefix}/{user}/{task}"


def task_dir(base_dir: Path, user_id: str, task_id: str) -> Path:
    """Return the local task folder: ``base_dir/user_id/task_id``."""
    user = _sanitize_id(user_id, field="user_id")
    task = _sanitize_id(task_id, field="task_id")
    return Path(base_dir) / user / task


def archive_key(cfg: "PersistenceConfig", user_id: str, task_id: str) -> str:
    """Return the archive key: ``{workspace_key}.tar.gz`` (latest-overwrites)."""
    return workspace_key(user_id, task_id, prefix=cfg.prefix) + ".tar.gz"
