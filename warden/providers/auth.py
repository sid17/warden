"""Provider auth resolution — the single place that answers 'what credential to
inject for which provider, from where'.

Env-based today: the process environment IS the seam. A container passes creds in
as env vars; a secret manager can pre-populate a mapping and pass it as ``env``.
Because ``resolve_auth`` accepts an explicit ``env`` argument, a container/secret
source can wrap or replace it later without touching any caller — no class
hierarchy or plugin registry needed.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

# Ordered per-provider auth env vars (first = preferred for messaging).
PROVIDER_AUTH_VARS: dict[str, tuple[str, ...]] = {
    "claude": ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"),
    "claude-cli": ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"),
    "codex": ("OPENAI_API_KEY",),
    "openharness": (),  # local Ollama — no cloud credential
}


def resolve_auth(
    provider: str, env: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Return the auth env vars to inject for ``provider`` — only those present in
    ``env`` (defaults to ``os.environ``).

    Pure: never reads files/Keychain, never mutates ``os.environ`` or ``env``. The
    environment IS the seam (a container passes creds in as env; a secret manager
    can pre-populate ``env``). Unknown providers resolve to ``{}``.
    """
    src = os.environ if env is None else env
    return {
        var: src[var]
        for var in PROVIDER_AUTH_VARS.get(provider, ())
        if src.get(var)
    }


def _codex_auth_present(env: Mapping[str, str]) -> bool:
    """True if a Codex ChatGPT-OAuth session exists on disk (``auth.json``).

    The ONLY place a filesystem probe is allowed — ``resolve_auth`` stays pure.
    Checks ``CODEX_HOME/auth.json`` (if ``CODEX_HOME`` is set) then the default
    ``~/.codex/auth.json``. The ``openai-codex`` binary reads this directly, so
    its presence means codex is authed even with no ``OPENAI_API_KEY`` (N4 fix).
    """
    codex_home = env.get("CODEX_HOME")
    candidates = []
    if codex_home:
        candidates.append(Path(codex_home) / "auth.json")
    candidates.append(Path.home() / ".codex" / "auth.json")
    return any(p.exists() for p in candidates)


def is_authed(provider: str, env: Mapping[str, str] | None = None) -> bool:
    """True if ``provider`` has a usable credential. ``openharness`` (local Ollama)
    is always True; unknown providers are False.

    N4 fix: ``codex`` is authed by EITHER an ``OPENAI_API_KEY`` (API-key mode) OR
    a ChatGPT-OAuth ``auth.json`` on disk (``CODEX_HOME/auth.json`` or
    ``~/.codex/auth.json``). The filesystem probe lives HERE only; ``resolve_auth``
    stays pure (it can only surface the env-based API key).
    """
    if provider == "openharness":
        return True
    if resolve_auth(provider, env):
        return True
    if provider == "codex":
        src = os.environ if env is None else env
        return _codex_auth_present(src)
    return False


def _fingerprint(secret: str) -> str:
    """A non-reversible tag for a credential — the last 4 chars only, never more.

    Enough to tell two keys apart in a log without ever exposing the secret.
    """
    s = secret.strip()
    return f"…{s[-4:]}" if len(s) >= 4 else "…"


def describe_auth(provider: str, env: Mapping[str, str] | None = None) -> dict:
    """Report the ACTIVE credential by mode + fingerprint — never the raw key (C7/AUTH-3).

    Returns ``{provider, mode, fingerprint, authed}`` where ``mode`` is one of
    ``oauth`` / ``api-key`` / ``file`` (codex ``auth.json``) / ``none``. The
    ``fingerprint`` is at most the last 4 chars of the token (``…abcd``) or a
    non-secret tag; it is safe to log. Pure except for codex's on-disk
    ``auth.json`` probe (same seam as :func:`is_authed`).
    """
    src = os.environ if env is None else env
    if provider == "openharness":
        return {"provider": provider, "mode": "none",
                "fingerprint": "local-ollama", "authed": True}
    if provider in ("claude", "claude-cli"):
        if src.get("CLAUDE_CODE_OAUTH_TOKEN") or src.get("ANTHROPIC_AUTH_TOKEN"):
            tok = src.get("CLAUDE_CODE_OAUTH_TOKEN") or src["ANTHROPIC_AUTH_TOKEN"]
            return {"provider": provider, "mode": "oauth",
                    "fingerprint": _fingerprint(tok), "authed": True}
        if src.get("ANTHROPIC_API_KEY"):
            return {"provider": provider, "mode": "api-key",
                    "fingerprint": _fingerprint(src["ANTHROPIC_API_KEY"]), "authed": True}
        return {"provider": provider, "mode": "none", "fingerprint": "", "authed": False}
    if provider == "codex":
        if src.get("OPENAI_API_KEY"):
            return {"provider": provider, "mode": "api-key",
                    "fingerprint": _fingerprint(src["OPENAI_API_KEY"]), "authed": True}
        if _codex_auth_present(src):
            return {"provider": provider, "mode": "file",
                    "fingerprint": "codex-auth.json", "authed": True}
        return {"provider": provider, "mode": "none", "fingerprint": "", "authed": False}
    return {"provider": provider, "mode": "none", "fingerprint": "", "authed": False}


def preflight(provider: str, env: Mapping[str, str] | None = None) -> dict:
    """Start-time readiness check for a provider (C8): ``{ok, reason, hint}``.

    ``ok`` is False with a human ``reason`` + ``hint`` when the provider cannot
    authenticate (creds unset). A local/subprocess provider (codex CLI missing,
    Ollama down, model not pulled) can extend this with a deeper probe later; the
    credential check is the always-safe baseline that needs no network.
    """
    if is_authed(provider, env):
        return {"ok": True, "reason": "", "hint": ""}
    return {
        "ok": False,
        "reason": f"{provider}: no usable credential",
        "hint": auth_hint(provider),
    }


def auth_hint(provider: str) -> str:
    """Human-readable hint on how to supply auth for ``provider`` — for CLI
    preflight and container docs."""
    if provider == "openharness":
        return "runs on local Ollama; no cloud credential."
    if provider in ("claude", "claude-cli"):
        return (
            "set CLAUDE_CODE_OAUTH_TOKEN (claude setup-token) "
            "or ANTHROPIC_API_KEY"
        )
    if provider == "codex":
        return "set OPENAI_API_KEY"
    return f"unknown provider {provider!r}: no known auth env var"
