"""pre-03 M0 · 3c/3e — injection: an ``AuthMethod`` → the env overlay to apply.

:func:`injection_env` is the type-dispatched producer of the ``auth_env`` overlay —
the ``{var: value}`` dict the provider then **strip-then-injects** into the child's
``env=`` (``BaseProvider.apply_auth_env``, unchanged). Dispatching on the typed
:data:`AuthMethod`:

  * ``OAuthToken`` / ``ApiKey`` → ``{var: secret}`` (secret resolved by reference,
    once, at the boundary; a missing secret fails loud — LAW 4, matching the legacy
    ``KeyRegistry.auth_env_for``).
  * ``SessionFile`` → ``{home_var: <dir>}`` — **file-mode auth as a first-class code
    path** (``CODEX_HOME`` set here), no longer a Docker-bed-only bind-mount hack. The
    ``home_var`` is NOT in the provider's credential strip list, so it survives the
    strip and lands in the child env.
  * ``Inherit`` → ``None`` — inject nothing; the child inherits the process credential.

This is the ONLY place a resolved secret VALUE is produced, and it is produced into a
narrow dict handed straight to the provider injector — never to ``os.environ``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from warden.harness_api.credentials.methods import (
    ApiKey,
    AuthMethod,
    Inherit,
    OAuthToken,
    SessionFile,
    _resolve_session_dir,
    get_secret,
)


def injection_env(
    method: AuthMethod, secrets: Mapping[str, str] | None = None
) -> dict[str, str] | None:
    """The env overlay to inject for ``method`` (``None`` ⇒ inherit process cred).

    ``secrets`` is the value source (defaults to :data:`os.environ`, read live at
    resolve time so a rotated secret takes effect on the next run). Raises
    ``ValueError`` when a referenced token/key is unset — a misconfiguration we must
    not paper over (LAW 4), the same fail-loud contract the legacy registry had.
    """
    src = os.environ if secrets is None else secrets
    if isinstance(method, Inherit):
        return None
    if isinstance(method, OAuthToken):
        value = get_secret(method.token_ref, src)
        if not value:
            raise ValueError(f"oauth token ref {method.token_ref!r} is not set")
        return {method.var: value}
    if isinstance(method, ApiKey):
        value = get_secret(method.key_ref, src)
        if not value:
            raise ValueError(f"api key ref {method.key_ref!r} is not set")
        return {method.var: value}
    if isinstance(method, SessionFile):
        home = _resolve_session_dir(method.path_ref, src)
        return {method.home_var: str(home)}
    raise ValueError(f"un-injectable auth method: {method!r}")  # LAW 4


def apply_method(
    env: dict[str, str],
    provider: str,
    method: AuthMethod,
    secrets: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The single per-run injector: strip-then-inject ``method`` into ``env``.

    Composes :func:`injection_env` (the typed overlay) with
    ``BaseProvider.apply_auth_env`` (strip every inherited credential var for
    ``provider`` from ``env``, THEN overlay) — so an ambient OAuth token cannot shadow
    an injected API key (the Claude transport prefers OAuth), and concurrent runs each
    carry exactly one credential with no bleed. :class:`Inherit` (``None`` overlay) is
    a no-op ⇒ the process credential is inherited. Mutates + returns the PASSED dict
    only (a per-spawn env copy), never :data:`os.environ`.
    """
    # Imported here to keep this module dependency-light + avoid an import cycle
    # (base_provider pulls the provider package).
    from warden.providers.base_provider import BaseProvider

    overlay = injection_env(method, secrets)
    return BaseProvider.apply_auth_env(env, provider, overlay)


__all__ = ["apply_method", "injection_env"]
