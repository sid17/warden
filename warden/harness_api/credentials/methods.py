"""pre-03 M0 · 3a — the typed credential model: ``AuthMethod`` + ``ResolvedCredential``.

The keystone of the auth module. Two current layers are auth-type-aware
(:mod:`warden.providers.auth`, but single-tenant) and multi-tenant
(:mod:`warden.harness_api.credentials.keys`, but auth-type-blind). They
unify behind ONE discriminated ``AuthMethod`` union — a closed set of typed
variants, one per credential *mechanism* — so auth type is a first-class thing,
not a string or an opaque dict (the field consensus: codex ``enum CodexAuth``,
LangFuse ``AuthMethod``; see ``docs/research/auth-management-research.md``).

Secrets stay **by reference**: an ``AuthMethod`` carries env-var *names*
(``*_ref``), never values. A value is resolved — through the prefix-dispatched
:func:`get_secret` — only at the injection boundary (3e) and, transiently, to
compute a non-reversible fingerprint here. :class:`ResolvedCredential` is the SAFE
descriptor ``resolve()`` returns (mode + fingerprint + authed, **no raw secret**);
its :meth:`~ResolvedCredential.describe` read surface mirrors
``providers/auth.py``'s ``describe_auth`` so there is **one** mode classifier.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Union

from warden.providers.auth import _fingerprint

# --- secrets by reference: the prefix-dispatched resolver --------------------

_OS_ENVIRON_PREFIX = "os.environ/"


def get_secret(ref: str, secrets: Mapping[str, str] | None = None) -> str | None:
    """Resolve a secret *reference* to its value, or ``None`` if unset.

    A ref is either a bare env-var name (``ANTHROPIC_API_KEY``) or a prefixed
    reference (``os.environ/ANTHROPIC_API_KEY``) — the LiteLLM ``get_secret`` shape.
    Only the ``os.environ/`` prefix (and the bare/implicit form, which means the
    same) is understood today; a future Vault / cloud-secret-manager backend
    registers an additional prefix HERE rather than rewriting every caller. The
    logical name lives in committable config; the value lives in the live process
    env (``secrets`` defaults to :data:`os.environ`).
    """
    src = os.environ if secrets is None else secrets
    name = ref[len(_OS_ENVIRON_PREFIX):] if ref.startswith(_OS_ENVIRON_PREFIX) else ref
    return src.get(name)


# --- the discriminated AuthMethod union --------------------------------------
#
# Each variant carries only REFERENCES (env-var names / a home path), never a
# secret value. ``kind`` is the discriminant (a closed set — the compiler/reader
# gets exhaustiveness). Frozen + hashable so a method can key a cache or a record.


@dataclass(frozen=True)
class OAuthToken:
    """An OAuth subscription token (e.g. ``CLAUDE_CODE_OAUTH_TOKEN``).

    ``var`` is the env var the token must be injected UNDER; ``token_ref`` is the
    reference the value resolves FROM (usually the same name, but decoupled so a
    Vault ref can back a differently-named injection var).
    """

    var: str
    token_ref: str
    kind: Literal["oauth"] = "oauth"


@dataclass(frozen=True)
class ApiKey:
    """A raw API key (``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY``)."""

    var: str
    key_ref: str
    kind: Literal["api_key"] = "api_key"


@dataclass(frozen=True)
class SessionFile:
    """An on-disk session credential (codex ``~/.codex/auth.json`` → ``CODEX_HOME``).

    ``home_var`` is the env var pointed at the directory HOLDING the session file;
    ``path_ref`` resolves to that directory — via :func:`get_secret` when it names
    an env var, else used as a literal filesystem path (``~`` expanded). The secret
    is the file's CONTENTS; the path is not a secret, so it may be a literal.
    """

    home_var: str
    path_ref: str
    kind: Literal["session_file"] = "session_file"


@dataclass(frozen=True)
class Inherit:
    """No managed credential — inherit whatever the process was launched with
    (the single-user dev/default case). The injector is a no-op for it."""

    kind: Literal["inherit"] = "inherit"


AuthMethod = Union[OAuthToken, ApiKey, SessionFile, Inherit]

# The ONE mode vocabulary — string-for-string identical to
# ``providers/auth.py describe_auth`` (``oauth`` / ``api-key`` / ``file`` / ``none``),
# so the union is a projection of the classifier, not a second one.
_MODE_BY_KIND: dict[str, str] = {
    "oauth": "oauth",
    "api_key": "api-key",
    "session_file": "file",
    "inherit": "none",
}


def mode_of(method: AuthMethod) -> str:
    """Project an :data:`AuthMethod` to its ``describe_auth`` mode string."""
    return _MODE_BY_KIND[method.kind]


def _resolve_session_dir(path_ref: str, secrets: Mapping[str, str]) -> Path:
    """The directory a :class:`SessionFile` points at: an env-var ref when it
    resolves, else the literal path (``~`` expanded)."""
    resolved = get_secret(path_ref, secrets)
    return Path(resolved or path_ref).expanduser()


# --- ResolvedCredential — the safe descriptor resolve() returns --------------


@dataclass(frozen=True)
class ResolvedCredential:
    """What ``resolve()`` returns: the chosen :data:`AuthMethod` (refs only), a
    non-reversible ``fingerprint``, and an ``authed`` flag — and **no raw secret**.

    The value is peeked ONCE (via :meth:`from_method`) to compute the fingerprint
    and the authed flag, and is never stored. ``denied_reason`` is set only when a
    policy gate (3c) rejected the credential (e.g. ``oauth_not_permitted_for_user``).
    """

    provider: str
    method: AuthMethod
    fingerprint: str
    authed: bool
    denied_reason: str | None = None

    @classmethod
    def from_method(
        cls,
        provider: str,
        method: AuthMethod,
        secrets: Mapping[str, str] | None = None,
        *,
        denied_reason: str | None = None,
    ) -> "ResolvedCredential":
        """Build the safe descriptor from a typed method + the secret source.

        Peeks the referenced secret only to fingerprint it (last-4, never stored)
        and to set ``authed``. For :class:`SessionFile`, ``authed`` is the on-disk
        presence of ``auth.json`` under the home dir (the only filesystem probe);
        the fingerprint is a non-secret tag. :class:`Inherit` carries no fingerprint
        and defers its authed-ness to the process credential (reported ``False``
        here — the injector leaves the inherited env in place).
        """
        src = os.environ if secrets is None else secrets
        fingerprint = ""
        authed = False
        if isinstance(method, OAuthToken):
            value = get_secret(method.token_ref, src)
            authed = bool(value)
            fingerprint = _fingerprint(value) if value else ""
        elif isinstance(method, ApiKey):
            value = get_secret(method.key_ref, src)
            authed = bool(value)
            fingerprint = _fingerprint(value) if value else ""
        elif isinstance(method, SessionFile):
            home = _resolve_session_dir(method.path_ref, src)
            authed = (home / "auth.json").exists()
            fingerprint = "codex-auth.json" if authed else ""
        # Inherit: authed stays False, fingerprint empty (process credential used).
        return cls(
            provider=provider,
            method=method,
            fingerprint=fingerprint,
            authed=authed,
            denied_reason=denied_reason,
        )

    def describe(self) -> dict:
        """The safe read surface: ``{provider, mode, fingerprint, authed}`` —
        the SAME shape (and mode vocabulary) as ``providers/auth.py describe_auth``,
        never a raw secret."""
        return {
            "provider": self.provider,
            "mode": mode_of(self.method),
            "fingerprint": self.fingerprint,
            "authed": self.authed,
        }


__all__ = [
    "ApiKey",
    "AuthMethod",
    "Inherit",
    "OAuthToken",
    "ResolvedCredential",
    "SessionFile",
    "get_secret",
    "mode_of",
]
