"""Persistence configuration for the v16 storage core.

Storage-agnostic: `base_dir` is where per-(user, task) folders live locally, and
`state_root` is the LocalFileBackend "mock S3" root where archives are written.
Both are driven by the same `workspace_key`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Default excludes: heavy/regenerable dirs and byte-compiled files.
# CRITICAL: do NOT add .git, .claude-home, or .codex here — resume needs them.
DEFAULT_EXCLUDE_PATTERNS: tuple[str, ...] = (
    "node_modules/",
    ".venv/",
    "__pycache__/",
    "*.pyc",
)


@dataclass
class PersistenceConfig:
    """Configuration for task persistence.

    Attributes:
        base_dir: Where task folders live locally (e.g. ``data/workspaces``).
        state_root: LocalFileBackend "mock S3" root (e.g. ``data/store``).
        prefix: Versioned key prefix; part of every workspace/archive key.
        exclude_patterns: Directory/file patterns dropped from backups. Never
            includes ``.git``/``.claude-home``/``.codex`` (resume needs them).
    """

    base_dir: Path
    state_root: Path
    prefix: str = "v1"
    exclude_patterns: tuple[str, ...] = field(
        default_factory=lambda: DEFAULT_EXCLUDE_PATTERNS
    )
