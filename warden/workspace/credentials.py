"""Out-of-band credential re-injection for persisted turns (A1).

Credentials are NEVER in the workspace snapshot — ``persistence.archive`` drops
them at archive time (A2). But Codex's OAuth ``auth.json`` must nonetheless exist
at ``$CODEX_HOME/auth.json`` for its SDK to authenticate. This module re-hydrates
it from an out-of-band, read-only source AFTER restore and BEFORE the turn — the
vendor-recommended Codex CI/CD flow (restore ``auth.json`` to ``$CODEX_HOME`` at
job start; the SDK refreshes it in place).

Design contract: ADR ``credential-backup-separation`` — exclude + re-inject.
Only codex keeps auth in a file today; Claude auth is the ``CLAUDE_CODE_OAUTH_TOKEN``
env var and OpenHarness (Ollama) needs no cloud credential, so both are no-ops.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# Files copied out-of-band into the pinned ``<task>/.codex``. ``auth.json`` is
# the OAuth credential; ``config.toml`` is non-secret but the SDK reads it from
# the same home, so we carry it alongside when present.
_CODEX_CRED_FILES = ("auth.json", "config.toml")


def codex_credential_source() -> Path | None:
    """Resolve the out-of-band codex credential source dir (a read-only mount).

    Precedence: ``WARDEN_CODEX_AUTH_SOURCE`` (explicit) → ambient ``CODEX_HOME``
    (the mounted credential) → ``~/.codex``. Returns the first that actually holds
    an ``auth.json``, else ``None``.

    Under persistence the orchestrator overrides ``CODEX_HOME`` ONLY in the codex
    subprocess env; the orchestrator process's own ``os.environ["CODEX_HOME"]``
    stays the mount, so this never resolves to the pinned ``<task>/.codex`` and
    never self-copies.
    """
    candidates = (
        os.environ.get("WARDEN_CODEX_AUTH_SOURCE"),
        os.environ.get("CODEX_HOME"),
        str(Path.home() / ".codex"),
    )
    for cand in candidates:
        if cand and (Path(cand) / "auth.json").is_file():
            return Path(cand)
    return None


def reinject_credentials(provider: str, task_dir: Path) -> list[str]:
    """Re-hydrate provider credentials into the pinned per-task home, out-of-band.

    Called on every persisted turn after restore. For codex, copies ``auth.json``
    (+ ``config.toml`` when present) from the out-of-band source into
    ``<task>/.codex`` so the SDK can authenticate against the pinned ``CODEX_HOME``
    even though the credential was excluded from the snapshot.

    Returns the basenames copied (``[]`` for non-codex providers or when no source
    is available — the codex turn then fails auth loudly rather than silently
    running unauthenticated, per LAW 4).
    """
    if provider != "codex":
        return []

    # Env-var lane: if OPENAI_API_KEY is set, codex prefers it and needs no file.
    if os.environ.get("OPENAI_API_KEY"):
        return []

    src = codex_credential_source()
    if src is None:
        logger.warning(
            "reinject_credentials(codex): no out-of-band credential source found "
            "(WARDEN_CODEX_AUTH_SOURCE / CODEX_HOME / ~/.codex all lack auth.json); "
            "the persisted codex turn will fail OAuth. Set OPENAI_API_KEY or mount "
            "a credential."
        )
        return []

    dst = Path(task_dir) / ".codex"
    dst.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in _CODEX_CRED_FILES:
        f = src / name
        if f.is_file():
            shutil.copy(f, dst / name)
            copied.append(name)
    logger.info(
        "reinject_credentials(codex): seeded %s into %s from %s",
        copied, dst, src,
    )
    return copied
