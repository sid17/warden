"""EXT-W1 (E3) — resolve an extracted seed bundle into a bootstrap payload.

Pure workspace logic: given a directory a seed bundle extracted to, return the
explicit allowlist :func:`~warden.workspace.bootstrap.bootstrap` consumes.
The harness owns placement; the product owns what's in the bundle.

Two seed shapes are supported (the resolver auto-detects):

- **Manifest-driven (current).** A ``seed-meta.json`` at the bundle root declares the
  generic ``copy`` list (``{path, to}`` — content stored under ``content/<path>``) plus
  ``workflows`` and ``mkdir``. This is how a product carries an arbitrary read-doc tree
  + empty write-dirs (the mkdir list has no content, so it travels as METADATA, not tar
  members). The core stays product-agnostic: it just lays what the metadata names.
- **Legacy dir-scan.** No ``seed-meta.json`` → the older top-level ``skills/``,
  ``agents/``, ``workflows/`` layout, mapped to skill/agent dirs + workflow ``*.yaml``.
  Kept so existing seeds/tests are unaffected.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SEED_META_NAME = "seed-meta.json"
_CONTENT_SUBDIR = "content"


class SeedManifestError(ValueError):
    """A ``seed-meta.json`` names an unsafe source arcname (absolute / ``..`` escape)."""


def _confined_source(content_root: Path, arcname: str) -> Path:
    """Resolve ``content_root/<arcname>`` and reject any escape outside ``content_root``.

    Symmetric to :func:`bootstrap._confined_dest` (which guards the DEST): a crafted
    ``seed-meta.json`` with ``path: "../../etc/passwd"`` must NOT let provision copy host
    files into a workspace. Absolute paths, NUL, and any ``..`` segment are rejected
    loudly (LAW 4) — a malicious/corrupt seed fails the provision, it is never silently
    honored."""
    rel = str(arcname).replace("\\", "/")
    if rel.startswith("/") or "\x00" in rel or any(seg == ".." for seg in rel.split("/")):
        raise SeedManifestError(
            f"seed-meta.json copy.path escapes the bundle (absolute/NUL/'..'): {arcname!r}"
        )
    base = content_root.resolve()
    src = (base / rel).resolve()
    if base != src and base not in src.parents:
        raise SeedManifestError(
            f"seed-meta.json copy.path escapes the bundle content root: {arcname!r}"
        )
    return content_root / rel


def resolve_seed(bundle_dir: Path) -> dict[str, Any]:
    """Resolve an extracted seed dir into the full bootstrap payload.

    Returns ``{workflows, skills, agents, copy_dirs, mkdirs}`` where:
      - ``workflows`` — ``*.yaml`` manifest file paths for ``.workflows/``.
      - ``skills``/``agents`` — legacy dir lists (empty in the manifest shape; the
        manifest carries them as ``copy_dirs`` instead).
      - ``copy_dirs`` — ``(source_path, dest_rel)`` pairs for the generic copy-list.
      - ``mkdirs`` — workspace-relative dirs to create empty.
    """
    bundle_dir = Path(bundle_dir)
    meta_path = bundle_dir / SEED_META_NAME
    if meta_path.is_file():
        return _resolve_manifest(bundle_dir, meta_path)
    return _resolve_legacy(bundle_dir)


def _resolve_manifest(bundle_dir: Path, meta_path: Path) -> dict[str, Any]:
    """Resolve the metadata-driven seed shape."""
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    content_root = bundle_dir / _CONTENT_SUBDIR

    workflows_root = bundle_dir / "workflows"
    workflows = (
        sorted(workflows_root.glob("*.yaml")) if workflows_root.is_dir() else []
    )

    copy_dirs: list[tuple[Path, str]] = []
    for entry in meta.get("copy", []):
        # ``path`` is the arcname under content/; ``to`` is the workspace-rel dest.
        # Confine the SOURCE (mirror of bootstrap's DEST guard) so a crafted
        # seed-meta.json cannot escape the bundle to read host files (fail-loud).
        src = _confined_source(content_root, entry["path"])
        copy_dirs.append((src, entry["to"]))

    return {
        "workflows": workflows,
        "skills": [],
        "agents": [],
        "copy_dirs": copy_dirs,
        "mkdirs": list(meta.get("mkdir", [])),
    }


def _resolve_legacy(bundle_dir: Path) -> dict[str, Any]:
    """Resolve the older top-level ``skills/``/``agents/``/``workflows/`` layout."""

    def _dirs(sub: str) -> list[Path]:
        root = bundle_dir / sub
        if not root.is_dir():
            return []
        return sorted(p for p in root.iterdir() if p.is_dir())

    workflows_root = bundle_dir / "workflows"
    workflows = (
        sorted(workflows_root.glob("*.yaml")) if workflows_root.is_dir() else []
    )
    return {
        "workflows": workflows,
        "skills": _dirs("skills"),
        "agents": _dirs("agents"),
        "copy_dirs": [],
        "mkdirs": [],
    }


def resolve_seed_dir(bundle_dir: Path) -> dict[str, list[Path]]:
    """Back-compat shim: the legacy ``{skills, agents, workflows}`` view.

    Preserved for callers/tests that predate the manifest shape. New code should use
    :func:`resolve_seed` (which also returns ``copy_dirs`` + ``mkdirs``)."""
    payload = resolve_seed(bundle_dir)
    return {
        "skills": payload["skills"],
        "agents": payload["agents"],
        "workflows": payload["workflows"],
    }


__all__ = ["resolve_seed", "resolve_seed_dir", "SEED_META_NAME", "SeedManifestError"]
