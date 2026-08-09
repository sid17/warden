"""``GET /file`` fixture store + path guard (§8, EXT-A1 / HR-11).

A run's artifacts live under a per-run root ``workspace_root/<run_id>/``. At run
start the script "writes" the workflow's canned fixtures there (a plain copy).
``read_file`` serves bytes through ``resolve_under_root`` — a realpath-under-root
guard that rejects ``..``/NUL/absolute/symlink-escape.
"""

from __future__ import annotations

import shutil
from pathlib import Path


class PathGuardError(Exception):
    """A path failed the traversal guard → maps to HTTP 400."""


class FileMissingError(Exception):
    """A guarded path resolved cleanly but no such file → maps to HTTP 404."""


def resolve_under_root(root: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``root``, rejecting any escape (§8).

    Raises ``PathGuardError`` on NUL / absolute / ``..``/symlink escape,
    ``FileMissingError`` if the resolved path is not an existing file.
    """
    if "\x00" in rel:
        raise PathGuardError("NUL byte in path")
    # An absolute path would escape the root outright.
    if Path(rel).is_absolute():
        raise PathGuardError("absolute path not allowed")
    real_root = root.resolve()
    candidate = (root / rel).resolve()  # realpath collapses .. and follows symlinks
    # Must be strictly under root (root itself is not a file to serve).
    if candidate != real_root and real_root not in candidate.parents:
        raise PathGuardError("path escapes run workspace")
    if not candidate.is_file():
        raise FileMissingError(f"no such file: {rel}")
    return candidate


class FixtureStore:
    """Per-run workspace roots seeded from canned fixtures.

    ``seed(run_id, workflow)`` copies ``fixture_dir/<workflow>/`` into
    ``workspace_root/<run_id>/`` (the script's "file writes"). ``read(run_id, rel)``
    serves guarded bytes.
    """

    def __init__(self, workspace_root: Path | str, fixture_dir: Path | str) -> None:
        self._workspace_root = Path(workspace_root)
        self._fixture_dir = Path(fixture_dir)

    def root_for(self, run_id: str) -> Path:
        return self._workspace_root / run_id

    def seed(self, run_id: str, workflow: str) -> None:
        """Copy the workflow's fixtures into the run's workspace. No-op if the
        workflow has no fixture dir (a script with no artifacts)."""
        dest = self.root_for(run_id)
        dest.mkdir(parents=True, exist_ok=True)
        src = self._fixture_dir / workflow
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True)

    def read(self, run_id: str, rel: str) -> bytes:
        """Read guarded bytes from a run's workspace (§8)."""
        root = self.root_for(run_id)
        path = resolve_under_root(root, rel)
        return path.read_bytes()

    def manifest(self, run_id: str) -> list[dict]:
        """Build a ``[{path, title, order}]`` manifest from a run's seeded files.

        Lists every ``.md`` artifact under the run workspace (sorted for
        determinism) and derives each entry's fields the way the REAL
        completion tool expects (task-2 §6): ``path`` is workspace-relative (so
        ``GET /file?path=`` serves it back), ``title`` is the file's first markdown
        heading (falling back to the filename stem), and ``order`` is the sorted
        index. Both the noop and profile invokers receive this identical manifest,
        keeping ``test_tool_seam_noop_vs_profile`` honest (D8).
        """
        root = self.root_for(run_id)
        if not root.is_dir():
            return []
        md_files = sorted(
            (p for p in root.rglob("*.md") if p.is_file()),
            key=lambda p: p.relative_to(root).as_posix(),
        )
        return [
            {
                "path": p.relative_to(root).as_posix(),
                "title": _title_from_md(p),
                "order": order,
            }
            for order, p in enumerate(md_files)
        ]


def _title_from_md(path: Path) -> str:
    """Return a file's first markdown heading, else its filename stem."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip() or path.stem
    except OSError:
        pass
    return path.stem
