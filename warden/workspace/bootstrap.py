"""Bootstrap a task folder from a declared skills/agents payload.

Standalone module (stdlib + optional PyYAML for frontmatter parsing). It scaffolds
a task's ``.claude/`` directory from an *explicit allowlist* of skill and agent
directories, then records a ``bootstrap.lock.json`` manifest for reproducible,
diffable verification.

Design contract: plan §4.3 / §9.2.
- Explicit allowlist copy (never a glob/scan of a source tree).
- Whole self-contained dirs copied via ``shutil.copytree`` (never flattened),
  preserving executable bits on ``scripts/*.sh`` and copying real files (no symlinks).
- Idempotent / ``skip_if_exists``: re-running adds missing entries but never
  clobbers an already-populated destination.
- Verify each declared entry landed AND that its frontmatter parses with both
  ``name`` and ``description`` present (a blank description silently breaks routing).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

try:  # PyYAML is present in the venv; parse frontmatter with it when available.
    import yaml  # type: ignore

    _HAS_YAML = True
except ImportError:  # pragma: no cover - fallback path exercised only without PyYAML
    _HAS_YAML = False

LOCKFILE_NAME = "bootstrap.lock.json"
_SKILLS_SUBDIR = Path(".claude") / "skills"
_AGENTS_SUBDIR = Path(".claude") / "agents"
# EXT-W1 (E3): the permission-manifest surface — flat ``*.yaml`` files, not dirs.
_WORKFLOWS_SUBDIR = Path(".workflows")


def _git_head(source: Path) -> str:
    """Best-effort ``git rev-parse HEAD`` in ``source``; empty string if unavailable."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(source),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _parse_frontmatter(md_path: Path) -> tuple[dict, str | None]:
    """Return (frontmatter_dict, error). Error is a string when parsing fails.

    Frontmatter is the leading block delimited by ``---`` lines. We never swallow a
    parse error (LAW 4): a malformed block is surfaced as an error string.
    """
    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError as exc:
        return {}, f"could not read {md_path}: {exc}"

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, f"{md_path}: missing YAML frontmatter (no leading '---')"

    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return {}, f"{md_path}: unterminated frontmatter (no closing '---')"

    block = "\n".join(lines[1:end_idx])

    if _HAS_YAML:
        try:
            data = yaml.safe_load(block) or {}
        except yaml.YAMLError as exc:
            return {}, f"{md_path}: frontmatter does not parse as YAML: {exc}"
        if not isinstance(data, dict):
            return {}, f"{md_path}: frontmatter is not a mapping"
        return data, None

    return _parse_frontmatter_fallback(block, md_path)


def _parse_frontmatter_fallback(block: str, md_path: Path) -> tuple[dict, str | None]:
    """Minimal ``key: value`` parser for the tiny toy frontmatter (no PyYAML path)."""
    data: dict = {}
    for raw in block.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            return {}, f"{md_path}: cannot parse frontmatter line: {raw!r}"
        key, _, value = line.partition(":")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        data[key.strip()] = value
    return data, None


def _copy_entry(source_dir: Path, dest_dir: Path, kind: str, problems: list[str]) -> str | None:
    """Copy one declared source dir to ``dest_dir`` (skip if already populated).

    Returns the entry name on success, or ``None`` if the source is unusable
    (recorded in ``problems``). Uses ``copytree`` so whole self-contained dirs are
    copied with mode bits preserved and no symlinks.
    """
    if not source_dir.is_dir():
        problems.append(f"{kind} source is not a directory: {source_dir}")
        return None

    name = source_dir.name
    if dest_dir.exists():
        # Idempotent: an already-populated destination is left untouched.
        return name

    # symlinks=False (default) copies real files, not symlinks. copytree preserves
    # mode bits (including +x) via copy2.
    shutil.copytree(source_dir, dest_dir, symlinks=False)
    return name


def _copy_workflow(source_file: Path, dest_file: Path, problems: list[str]) -> str | None:
    """Copy one declared ``*.yaml`` manifest to ``dest_file`` (skip if it exists).

    Workflows are flat files, not dirs, so ``_copy_entry``'s ``copytree`` cannot be
    reused (E3 gotcha #2) — use ``shutil.copy2`` (mode bits preserved). Returns the
    workflow *name* (the file stem) on success, or ``None`` if the source is unusable.
    """
    if not source_file.is_file():
        problems.append(f"workflow source is not a file: {source_file}")
        return None
    if dest_file.exists():
        return dest_file.stem  # idempotent: never clobber an existing manifest
    shutil.copy2(source_file, dest_file)
    return dest_file.stem


def _confined_dest(target_dir: Path, dest_rel: str, problems: list[str]) -> Path | None:
    """Resolve ``target_dir/<dest_rel>`` and reject any escape outside ``target_dir``.

    The generic copy/mkdir lists take manifest-supplied ``to`` paths; confine them so a
    malformed manifest (absolute path, ``..`` traversal) can never write outside the
    task box (LAW 4 — surfaced in ``problems``, never silently skipped)."""
    rel = str(dest_rel).replace("\\", "/")
    if rel.startswith("/") or "\x00" in rel or any(
        seg == ".." for seg in rel.split("/")
    ):
        problems.append(f"unsafe destination path (absolute/NUL/'..'): {dest_rel!r}")
        return None
    base = target_dir.resolve()
    dest = (base / rel).resolve()
    if base != dest and base not in dest.parents:
        problems.append(f"destination escapes the workspace: {dest_rel!r}")
        return None
    return target_dir / rel


def _copy_path(source: Path, dest: Path, problems: list[str]) -> str | None:
    """Copy one declared source (a dir OR a file) to ``dest`` (skip if it exists).

    The generic copy-list primitive (Option B): a manifest names ``{from, to}`` pairs
    and the harness lays each verbatim — a dir via ``copytree`` (mode bits preserved,
    no symlinks), a file via ``copy2``. Parent dirs are created. Idempotent: an already
    -populated destination is left untouched (never clobber produced work). Returns the
    ``to`` rel-path string on success, or ``None`` if the source is unusable
    (recorded in ``problems``, LAW 4)."""
    if not source.exists():
        problems.append(f"copy source does not exist: {source}")
        return None
    if dest.exists():
        return None  # idempotent: leave an existing destination as-is
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, dest, symlinks=False)
    else:
        shutil.copy2(source, dest)
    return str(dest)


def bootstrap(
    target_dir: Path,
    skills: list[Path],
    agents: list[Path],
    source_ref: str = "",
    workflows: list[Path] | None = None,
    copy_dirs: list[tuple[Path, str]] | None = None,
    mkdirs: list[str] | None = None,
) -> dict:
    """Scaffold ``target_dir/.claude/`` + ``.workflows/`` from declared payloads.

    Args:
        target_dir: destination task folder (created if missing).
        skills: explicit allowlist of skill source directories to copy.
        agents: explicit allowlist of agent source directories to copy.
        source_ref: opaque provenance string recorded in the lockfile.
        workflows: explicit allowlist of ``*.yaml`` manifest files to place under
            ``.workflows/`` (EXT-W1) — the permission surface the run binds to.
        copy_dirs: GENERIC copy-list — ``(source_path, dest_rel)`` pairs laid verbatim
            into ``target_dir/<dest_rel>`` (dir → copytree, file → copy2). Product-
            agnostic: the *manifest* supplies the paths; the core knows no product
            nouns. Default empty → existing callers/tests unaffected.
        mkdirs: GENERIC mkdir-list — workspace-relative dirs created empty (the skill
            WRITES into them, e.g. ``courses``). Default empty.

    Returns:
        Summary dict: ``{target, skills, agents, workflows, copied, mkdirs, lockfile,
        problems}``.

    Raises:
        RuntimeError: if any declared entry failed to land at its destination.
    """
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    workflows = list(workflows or [])
    copy_dirs = list(copy_dirs or [])
    mkdirs = list(mkdirs or [])

    skills_root = target_dir / _SKILLS_SUBDIR
    agents_root = target_dir / _AGENTS_SUBDIR
    workflows_root = target_dir / _WORKFLOWS_SUBDIR
    skills_root.mkdir(parents=True, exist_ok=True)
    agents_root.mkdir(parents=True, exist_ok=True)
    workflows_root.mkdir(parents=True, exist_ok=True)

    problems: list[str] = []
    copied_skills: list[str] = []
    copied_agents: list[str] = []
    copied_workflows: list[str] = []
    copied_paths: list[str] = []
    made_dirs: list[str] = []

    for skill_src in skills:
        skill_src = Path(skill_src)
        name = _copy_entry(skill_src, skills_root / skill_src.name, "skill", problems)
        if name is not None:
            copied_skills.append(name)

    for agent_src in agents:
        agent_src = Path(agent_src)
        name = _copy_entry(agent_src, agents_root / agent_src.name, "agent", problems)
        if name is not None:
            copied_agents.append(name)

    for wf_src in workflows:
        wf_src = Path(wf_src)
        name = _copy_workflow(wf_src, workflows_root / wf_src.name, problems)
        if name is not None:
            copied_workflows.append(name)

    # GENERIC copy-list: lay each declared {from, to} verbatim (confined under target).
    for source, dest_rel in copy_dirs:
        dest = _confined_dest(target_dir, dest_rel, problems)
        if dest is None:
            continue
        landed = _copy_path(Path(source), dest, problems)
        if landed is not None:
            copied_paths.append(dest_rel)

    # GENERIC mkdir-list: create each declared empty dir (confined under target).
    for rel in mkdirs:
        dest = _confined_dest(target_dir, rel, problems)
        if dest is None:
            continue
        dest.mkdir(parents=True, exist_ok=True)
        made_dirs.append(rel)

    # Verify each declared entry actually landed at its destination.
    for name in copied_skills:
        if not (skills_root / name).is_dir():
            problems.append(f"skill did not land at destination: {name}")
    for name in copied_agents:
        if not (agents_root / name).is_dir():
            problems.append(f"agent did not land at destination: {name}")
    for name in copied_workflows:
        if not (workflows_root / f"{name}.yaml").is_file():
            problems.append(f"workflow did not land at destination: {name}")
    for rel in copied_paths:
        if not (target_dir / rel).exists():
            problems.append(f"copy entry did not land at destination: {rel}")
    for rel in made_dirs:
        if not (target_dir / rel).is_dir():
            problems.append(f"mkdir did not create destination: {rel}")

    # Determine a git commit best-effort from the first declared source's repo.
    commit = ""
    first_source = skills[0] if skills else (agents[0] if agents else None)
    if first_source is not None:
        commit = _git_head(Path(first_source).resolve().parent)

    lockfile = target_dir / LOCKFILE_NAME
    # Never write secrets: only provenance and the declared entry names.
    lock_data = {
        "source_ref": source_ref,
        "commit": commit,
        "skills": sorted(copied_skills),
        "agents": sorted(copied_agents),
        "workflows": sorted(copied_workflows),
        "copied": sorted(copied_paths),
        "mkdirs": sorted(made_dirs),
    }

    # Write the lockfile only on success: a failed bootstrap must not leave a
    # lockfile describing a partial/failed placement.
    if problems:
        raise RuntimeError(
            "bootstrap failed to place all declared entries: " + "; ".join(problems)
        )

    lockfile.write_text(json.dumps(lock_data, indent=2) + "\n", encoding="utf-8")

    return {
        "target": str(target_dir),
        "skills": sorted(copied_skills),
        "agents": sorted(copied_agents),
        "workflows": sorted(copied_workflows),
        "copied": sorted(copied_paths),
        "mkdirs": sorted(made_dirs),
        "lockfile": str(lockfile),
        "problems": problems,
    }


def verify_bootstrap(target_dir: Path) -> list[str]:
    """Return a list of problem strings for a bootstrapped ``target_dir`` (empty = OK).

    Checks that every ``.claude/skills/*/SKILL.md`` and ``.claude/agents/*/*.md``
    frontmatter parses and carries both ``name`` and ``description``. A missing or
    blank ``description`` is a problem (it silently breaks routing).
    """
    target_dir = Path(target_dir)
    problems: list[str] = []

    skills_root = target_dir / _SKILLS_SUBDIR
    agents_root = target_dir / _AGENTS_SUBDIR

    def _check(md_path: Path) -> None:
        data, error = _parse_frontmatter(md_path)
        if error is not None:
            problems.append(error)
            return
        name = data.get("name")
        description = data.get("description")
        if not name or (isinstance(name, str) and not name.strip()):
            problems.append(f"{md_path}: frontmatter missing or blank 'name'")
        if not description or (isinstance(description, str) and not description.strip()):
            problems.append(f"{md_path}: frontmatter missing or blank 'description'")

    if skills_root.is_dir():
        for skill_dir in sorted(p for p in skills_root.iterdir() if p.is_dir()):
            md = skill_dir / "SKILL.md"
            if not md.is_file():
                problems.append(f"skill missing SKILL.md: {skill_dir}")
                continue
            _check(md)

    if agents_root.is_dir():
        for agent_dir in sorted(p for p in agents_root.iterdir() if p.is_dir()):
            md_files = sorted(agent_dir.glob("*.md"))
            if not md_files:
                problems.append(f"agent missing a *.md definition: {agent_dir}")
                continue
            for md in md_files:
                _check(md)

    return problems
