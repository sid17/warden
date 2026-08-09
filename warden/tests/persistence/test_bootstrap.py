"""Tests for the bootstrap scaffolder (plan §4.3 / §9.7).

Covers: allowlist copy, lockfile contents, frontmatter verify (good + bad),
``+x`` preservation on ``scripts/count.sh``, and idempotency (no clobber).

These tests are plain synchronous — ``bootstrap`` is synchronous.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from warden.workspace.bootstrap import (
    LOCKFILE_NAME,
    bootstrap,
    verify_bootstrap,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "skills"
COUNTING = FIXTURES / "counting"
PHONETIC = FIXTURES / "phonetic"


def _skills_root(target: Path) -> Path:
    return target / ".claude" / "skills"


def test_allowlist_copy_lands_skill(tmp_path: Path) -> None:
    target = tmp_path / "task"
    bootstrap(target, skills=[COUNTING, PHONETIC], agents=[])

    counting_md = _skills_root(target) / "counting" / "SKILL.md"
    phonetic_md = _skills_root(target) / "phonetic" / "SKILL.md"
    assert counting_md.is_file()
    assert phonetic_md.is_file()


def test_only_declared_skills_are_copied(tmp_path: Path) -> None:
    # Allowlist behavior: declaring only `phonetic` must not pull in `counting`,
    # even though both live side-by-side in the fixtures dir.
    target = tmp_path / "task"
    bootstrap(target, skills=[PHONETIC], agents=[])

    assert (_skills_root(target) / "phonetic").is_dir()
    assert not (_skills_root(target) / "counting").exists()


def test_lockfile_written_with_expected_names(tmp_path: Path) -> None:
    target = tmp_path / "task"
    summary = bootstrap(
        target, skills=[COUNTING, PHONETIC], agents=[], source_ref="unit-test"
    )

    lockfile = Path(summary["lockfile"])
    assert lockfile.name == LOCKFILE_NAME
    data = json.loads(lockfile.read_text())

    assert data["source_ref"] == "unit-test"
    assert data["skills"] == ["counting", "phonetic"]
    assert data["agents"] == []
    assert "commit" in data  # best-effort; may be "" outside a git repo


def test_lockfile_has_no_secrets(tmp_path: Path) -> None:
    target = tmp_path / "task"
    summary = bootstrap(target, skills=[COUNTING], agents=[])
    raw = Path(summary["lockfile"]).read_text().lower()
    for forbidden in ("token", "api_key", "oauth", "secret", "password"):
        assert forbidden not in raw


def test_verify_ok_for_good_bootstrap(tmp_path: Path) -> None:
    target = tmp_path / "task"
    bootstrap(target, skills=[COUNTING, PHONETIC], agents=[])
    assert verify_bootstrap(target) == []


def test_verify_flags_blank_description(tmp_path: Path) -> None:
    # Build a bad fixture: a SKILL.md with a name but a blank description.
    target = tmp_path / "task"
    bad_dir = _skills_root(target) / "broken"
    bad_dir.mkdir(parents=True)
    (bad_dir / "SKILL.md").write_text(
        '---\nname: broken\ndescription: ""\n---\n\nbody\n', encoding="utf-8"
    )

    problems = verify_bootstrap(target)
    assert problems, "expected a problem for the blank description"
    assert any("description" in p for p in problems)


def test_verify_flags_missing_description(tmp_path: Path) -> None:
    target = tmp_path / "task"
    bad_dir = _skills_root(target) / "nodescr"
    bad_dir.mkdir(parents=True)
    (bad_dir / "SKILL.md").write_text(
        "---\nname: nodescr\n---\n\nbody\n", encoding="utf-8"
    )

    problems = verify_bootstrap(target)
    assert any("description" in p for p in problems)


def test_executable_bit_preserved(tmp_path: Path) -> None:
    target = tmp_path / "task"
    bootstrap(target, skills=[COUNTING], agents=[])

    dest = _skills_root(target) / "counting" / "scripts" / "count.sh"
    assert dest.is_file()
    assert os.access(dest, os.X_OK), "executable bit not preserved by copytree"
    assert not dest.is_symlink(), "must copy real files, not symlinks"


def test_idempotent_second_bootstrap_does_not_clobber(tmp_path: Path) -> None:
    target = tmp_path / "task"
    bootstrap(target, skills=[COUNTING], agents=[])

    # Simulate a user edit inside the already-populated skill dir.
    edited = _skills_root(target) / "counting" / "SKILL.md"
    edited.write_text("USER EDITED CONTENT\n", encoding="utf-8")

    # Re-run: the existing populated dir must be left untouched.
    bootstrap(target, skills=[COUNTING, PHONETIC], agents=[])

    assert edited.read_text() == "USER EDITED CONTENT\n"
    # ...while a newly-declared skill is still added.
    assert (_skills_root(target) / "phonetic" / "SKILL.md").is_file()


# --- EXT-W1: bootstrap places .workflows/ -------------------------------------


def _make_workflow(tmp: Path, name: str = "dummy") -> Path:
    wf = tmp / "src" / f"{name}.yaml"
    wf.parent.mkdir(parents=True, exist_ok=True)
    wf.write_text(
        f"name: {name}\ndescription: d\npermissions:\n  mode: auto\n",
        encoding="utf-8",
    )
    return wf


def test_bootstrap_places_workflows(tmp_path: Path) -> None:
    target = tmp_path / "task"
    wf = _make_workflow(tmp_path)
    summary = bootstrap(target, skills=[COUNTING], agents=[], workflows=[wf])

    # The manifest landed as a flat file under .workflows/ (new assertion).
    assert (target / ".workflows" / "dummy.yaml").is_file()
    assert (_skills_root(target) / "counting" / "SKILL.md").is_file()
    # The ack (return dict) lists the workflow name (its stem).
    assert summary["workflows"] == ["dummy"]
    assert summary["skills"] == ["counting"]
    # ...and the lockfile records it.
    data = json.loads(Path(summary["lockfile"]).read_text())
    assert data["workflows"] == ["dummy"]


def test_reprovision_does_not_clobber_workflow(tmp_path: Path) -> None:
    target = tmp_path / "task"
    wf = _make_workflow(tmp_path)
    bootstrap(target, skills=[], agents=[], workflows=[wf])

    landed = target / ".workflows" / "dummy.yaml"
    landed.write_text("EDITED MANIFEST\n", encoding="utf-8")
    # Re-provision: the existing manifest is left untouched (idempotent).
    bootstrap(target, skills=[], agents=[], workflows=[wf])
    assert landed.read_text() == "EDITED MANIFEST\n"


def test_bootstrap_raises_on_missing_source(tmp_path: Path) -> None:
    target = tmp_path / "task"
    with pytest.raises(RuntimeError):
        bootstrap(target, skills=[tmp_path / "does-not-exist"], agents=[])


def test_no_lockfile_written_when_bootstrap_fails(tmp_path: Path) -> None:
    # A bad (non-dir) skill source must fail the bootstrap AND leave no lockfile:
    # a lockfile would falsely describe a partial/failed placement as complete.
    target = tmp_path / "task"
    bad_source = tmp_path / "not-a-dir.txt"
    bad_source.write_text("i am a file, not a skill dir\n", encoding="utf-8")

    with pytest.raises(RuntimeError):
        bootstrap(target, skills=[bad_source], agents=[])

    assert not (target / LOCKFILE_NAME).exists()
