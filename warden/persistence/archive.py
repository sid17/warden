"""Shared gzip-tar archive helpers used by every storage backend.

Pure functions with no I/O policy of their own: the caller owns *where* the
archive lives (tempfile placement, atomic replace, upload/download). These
helpers only build/extract the tarball and enforce the exclusion + safety rules
that every backend must apply identically:

* ``_ALWAYS_KEEP`` (``.git`` / ``.claude-home`` / ``.codex``) is NEVER excluded
  — resume depends on those dirs, whatever the exclude config says.
* EXCEPT provider credential files (``auth.json`` / ``.credentials.json``) under a
  pinned provider home, which are ALWAYS dropped (checked before ``_ALWAYS_KEEP``)
  — secrets are never backup data; they are re-injected out-of-band on restore
  (ADR credential-backup-separation).
* extraction refuses any member whose resolved path escapes the destination
  (path-traversal guard), surfacing a ``WorkspaceRestoreError`` rather than
  writing outside ``local_dir``.
"""

from __future__ import annotations

import fnmatch
import os
import tarfile
from pathlib import Path

from warden.persistence.backend import WorkspaceRestoreError

# Never excluded, whatever the config says — resume depends on these.
_ALWAYS_KEEP = (".git", ".claude-home", ".codex")

# Credentials are NEVER backed up (ADR: credential-backup-separation). These
# basenames are provider secrets that live INSIDE a pinned provider home
# (e.g. ``<task>/.codex/auth.json``); excluding them keeps the OAuth token out of
# every tarball (local + S3). They are re-injected out-of-band on restore
# (``workspace.credentials.reinject_credentials``) — exclude + re-inject.
_CREDENTIAL_BASENAMES = ("auth.json", ".credentials.json")
# The pinned provider-home dirs a credential can legitimately sit under. Scoping
# the drop to these avoids nuking an unrelated user file that happens to be
# named ``auth.json`` somewhere in the repo.
_PROVIDER_HOME_DIRS = (".codex", ".claude-home", ".openharness", ".claude")


def _is_credential(parts: list[str], *, is_dir: bool) -> bool:
    """Return True if ``parts`` is a provider credential file to keep out of backups.

    A credential is a known secret basename (``auth.json`` / ``.credentials.json``)
    sitting under a pinned provider-home segment. Checked BEFORE ``_ALWAYS_KEEP``
    so a ``.codex/auth.json`` cannot be force-kept by the always-keep rule.
    """
    if is_dir:
        return False
    return parts[-1] in _CREDENTIAL_BASENAMES and any(
        seg in _PROVIDER_HOME_DIRS for seg in parts[:-1]
    )


def _is_excluded(
    rel_path: str, exclude_patterns: tuple[str, ...], *, is_dir: bool
) -> bool:
    """Return True if ``rel_path`` (relative, POSIX-style) is excluded.

    Credential files are dropped first (never in a backup). Otherwise any path
    segment matching an always-keep name short-circuits to False.
    """
    parts = rel_path.split("/")
    if _is_credential(parts, is_dir=is_dir):
        return True
    if any(p in _ALWAYS_KEEP for p in parts):
        return False
    for pat in exclude_patterns:
        dir_pat = pat.rstrip("/")
        if pat.endswith("/"):
            # Directory pattern: match any segment (drops the dir subtree).
            if any(fnmatch.fnmatch(seg, dir_pat) for seg in parts):
                return True
        else:
            # File pattern: match the basename.
            if fnmatch.fnmatch(parts[-1], pat):
                return True
    return False


def _link_escapes(tarinfo: tarfile.TarInfo) -> bool:
    """Return True if a (sym|hard)link member points outside the archive tree.

    Absolute targets always escape. A relative target escapes if, resolved
    against the member's own directory (POSIX-style, purely lexical), it climbs
    above the archive root. Such links can never be restored safely into a fresh
    workspace, so they are dropped at archive time.
    """
    target = tarinfo.linkname
    if not target:
        return False
    if target.startswith("/"):
        return True
    # member dir + relative target, collapsed lexically; escapes if depth < 0.
    member_dir = tarinfo.name.rsplit("/", 1)[0] if "/" in tarinfo.name else ""
    parts = [p for p in member_dir.split("/") if p not in ("", ".")]
    for seg in target.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if not parts:
                return True  # climbed above the archive root
            parts.pop()
        else:
            parts.append(seg)
    return False


def write_tar_gz(
    local_dir: Path, dest_path: Path, exclude_patterns: tuple[str, ...]
) -> dict:
    """Build a gzip tarball of ``local_dir`` at ``dest_path``.

    The caller owns atomicity and tempfile placement; this just writes the
    archive to ``dest_path`` (which should already be a tempfile the caller
    will move/upload). Applies the exclusion + ``_ALWAYS_KEEP`` rules.

    Returns:
        ``{"files": int, "bytes": int}`` counting archived regular files.
    """
    local_dir = Path(local_dir)
    files = 0
    total_bytes = 0

    def _keep(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
        nonlocal files, total_bytes
        rel = tarinfo.name
        if rel in ("", "."):
            return tarinfo  # archive root entry
        if _is_excluded(rel, exclude_patterns, is_dir=tarinfo.isdir()):
            return None
        # Drop symlinks that point outside the archive (absolute target, or a
        # relative target that escapes the member's own directory). These are
        # ephemeral scratch — codex extracts arg0 helper binaries under
        # ``.codex/tmp`` as absolute-target symlinks — never restorable and
        # rejected by a hardened tar extractor on restore. Regenerable, so
        # dropping them is safe; the resume-critical transcript/auth are not
        # symlinks and are kept.
        if (tarinfo.issym() or tarinfo.islnk()) and _link_escapes(tarinfo):
            return None
        if tarinfo.isfile():
            files += 1
            total_bytes += tarinfo.size
        return tarinfo

    # Manual os.walk (not ``tar.add(recursive=True)``) for two reasons:
    #   1. Prune excluded dir subtrees so we never descend into provider scratch
    #      (e.g. codex's ``.codex/tmp`` / ``.codex/.tmp`` git-clone workdirs).
    #   2. Be race-safe: those scratch dirs churn lockfiles that can vanish
    #      between the walk and the ``lstat`` — ``tar.add`` would crash the whole
    #      snapshot; here a vanished entry is simply skipped.
    with tarfile.open(dest_path, "w:gz") as tar:
        tar.add(str(local_dir), arcname=".", recursive=False)  # root dir entry
        for dirpath, dirnames, filenames in os.walk(str(local_dir)):
            rel_dir = os.path.relpath(dirpath, str(local_dir))
            # Prune excluded subdirs in place so os.walk won't descend into them.
            kept_dirs = []
            for d in dirnames:
                rel = d if rel_dir == "." else f"{rel_dir}/{d}"
                if _is_excluded(rel.replace(os.sep, "/"), exclude_patterns, is_dir=True):
                    continue
                kept_dirs.append(d)
            dirnames[:] = kept_dirs
            # Add kept dir entries (preserve empty dirs / dir metadata).
            for d in dirnames:
                _add_member(tar, os.path.join(dirpath, d),
                            _arcname(local_dir, dirpath, d), _keep)
            for f in filenames:
                _add_member(tar, os.path.join(dirpath, f),
                            _arcname(local_dir, dirpath, f), _keep)

    return {"files": files, "bytes": total_bytes}


def _arcname(local_dir: Path, dirpath: str, name: str) -> str:
    """POSIX arcname (``./…``) for ``dirpath/name`` relative to ``local_dir``."""
    rel = os.path.relpath(os.path.join(dirpath, name), str(local_dir))
    return "./" + rel.replace(os.sep, "/")


def _add_member(tar, fullpath, arcname, keep):
    """Add one non-recursive member, applying ``keep``; skip if it vanished.

    Provider scratch dirs churn lockfiles that can disappear between the walk
    and the ``lstat`` inside ``tar.add`` — a ``FileNotFoundError`` there would
    abort the whole snapshot. Such entries are ephemeral, so we skip them (LAW 4:
    the loss is intentional and scoped to regenerable scratch, not swallowed).
    """
    try:
        tar.add(fullpath, arcname=arcname, recursive=False, filter=keep)
    except FileNotFoundError:
        pass


def extract_tar_gz(src_path: Path, local_dir: Path, key: str) -> dict:
    """Extract the gzip tarball at ``src_path`` into ``local_dir``.

    Uses the path-traversal guard; any unsafe or corrupt archive raises
    ``WorkspaceRestoreError`` (never swallowed, never partially written outside
    ``local_dir``).

    Returns:
        ``{"files": int, "bytes": int}`` counting extracted regular files.
    """
    local_dir = Path(local_dir)
    local_dir.mkdir(parents=True, exist_ok=True)

    files = 0
    total_bytes = 0
    try:
        with tarfile.open(src_path, "r:gz") as tar:
            members = tar.getmembers()
            for m in members:
                if m.isfile():
                    files += 1
                    total_bytes += m.size
            _safe_extractall(tar, members, local_dir)
    except WorkspaceRestoreError:
        raise
    except Exception as exc:  # surface as a restore failure, don't swallow
        raise WorkspaceRestoreError(
            f"failed to extract archive {key}: {exc}",
            key=key,
            retries_attempted=1,
            last_error=exc,
        ) from exc

    return {"files": files, "bytes": total_bytes}


def validate_member_path(member_path: str) -> str:
    """EXT-A1 — validate a confined sub-path for a single-file read.

    ``_sanitize_id`` (``persistence/keys.py``) validates a single id *segment*, not a
    multi-segment sub-path, so A1 needs this distinct validator. Rejects empty, NUL,
    absolute paths, and any ``..`` traversal; normalizes to a POSIX relative path
    (leading ``./`` and redundant separators collapsed).

    Raises:
        ValueError: the path is empty, absolute, contains NUL, or traverses up.
    """
    if not member_path or not member_path.strip():
        raise ValueError("file path must be non-empty")
    if "\x00" in member_path:
        raise ValueError("file path must not contain NUL")
    p = member_path.replace("\\", "/")
    if p.startswith("/"):
        raise ValueError(f"file path must be relative, not absolute: {member_path!r}")
    segments = [s for s in p.split("/") if s not in ("", ".")]
    if any(s == ".." for s in segments):
        raise ValueError(f"file path must not contain '..': {member_path!r}")
    if not segments:
        raise ValueError(f"file path resolves to empty: {member_path!r}")
    return "/".join(segments)


def read_member_from_tar(tar_path: Path, member_path: str) -> bytes:
    """EXT-A1 — read the bytes of ONE confined member from a gzip tarball.

    Shared by both the local and S3 backends (each hands a tarball path — the S3
    backend downloads to a tempfile first). ``member_path`` is validated
    (``validate_member_path``); the member is looked up by its archive arcname
    (``./`` prefix, as ``write_tar_gz`` writes it); a directory, a missing member,
    or an escaping link is rejected. Reuses ``_link_escapes`` for link safety.

    Raises:
        ValueError: the path fails confinement (``..``/NUL/absolute/escaping link).
        FileNotFoundError: the member does not exist (or is a directory).
    """
    rel = validate_member_path(member_path)
    with tarfile.open(tar_path, "r:gz") as tar:
        member = None
        for candidate in ("./" + rel, rel):
            try:
                member = tar.getmember(candidate)
                break
            except KeyError:
                continue
        if member is None:
            raise FileNotFoundError(f"file not found in archive: {member_path}")
        if member.isdir():
            raise FileNotFoundError(f"path is a directory, not a file: {member_path}")
        if (member.issym() or member.islnk()) and _link_escapes(member):
            raise ValueError(f"file path resolves to an escaping link: {member_path!r}")
        extracted = tar.extractfile(member)
        if extracted is None:
            raise FileNotFoundError(f"file not readable in archive: {member_path}")
        return extracted.read()


def _safe_extractall(
    tar: tarfile.TarFile, members: list[tarfile.TarInfo], dest: Path
) -> None:
    """Extract ``members`` into ``dest``, refusing any path escaping ``dest``.

    Guards against path-traversal in a malicious/corrupt archive.
    """
    dest_resolved = dest.resolve()
    for m in members:
        target = (dest / m.name).resolve()
        if dest_resolved not in target.parents and target != dest_resolved:
            raise WorkspaceRestoreError(
                f"unsafe path in archive: {m.name}",
                key="",
                retries_attempted=0,
                last_error=None,
            )
    # Drop any escaping (sym|hard)link members that predate the write-time
    # filter (e.g. codex's absolute-target ``.codex/tmp`` arg0 helpers). Python
    # 3.12+'s default ``data`` extraction filter would raise on these; they are
    # regenerable scratch, so we omit them rather than fail the whole restore.
    safe = [m for m in members if not (m.issym() or m.islnk()) or not _link_escapes(m)]
    # Explicit ``data`` filter (the Python 3.12+ default; required in 3.14). Our
    # traversal guard + escaping-link strip above already removed the members it
    # would reject, so remaining in-tree members extract faithfully.
    tar.extractall(str(dest), members=safe, filter="data")
