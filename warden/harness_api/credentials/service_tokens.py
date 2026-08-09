"""EXT-T1a — the caller-authentication token registry (harness-owned).

Distinct from provider-credential resolution (``keys.py`` / the ``AuthResolver``,
§10): that answers *which key runs the model*; this answers *which backend is
calling the Runs API*. A per-service token (``x-service-token``) authenticates the
caller before any run-scoped work.

Config is a flat ``service_name → token`` map, loaded with the EXACT inline-wins
pattern of :class:`~warden.harness_api.credentials.keys.KeyRegistry`
(``SERVICE_TOKENS_JSON`` inline → ``SERVICE_TOKENS_FILE`` path → empty). The map is
safe to commit only if the tokens are injected by reference; here the tokens ARE the
secret, so this config is operator-mounted, never committed with real values.

**Empty registry ⇒ open** (single-tenant dev default), symmetric with "no
``KeyRegistry`` ⇒ inherit the process credential". A malformed config is a hard
error (LAW 4) so a bad deploy fails loudly rather than silently opening the API.

Config shape::

    {"my-app": "tok-abc123", "another-service": "tok-def456"}
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from warden.harness_api.config import CallerAuthConfig


class ServiceTokenRegistry:
    """Resolves ``x-service-token → is this a known caller?``.

    Pure w.r.t. ``os.environ``: it holds the token map given at construction and
    never reads the environment. An empty map means "open" — every call is allowed
    (the dev/single-tenant default).
    """

    def __init__(self, tokens: Mapping[str, str]) -> None:
        self._tokens = dict(tokens)
        # A set of accepted token *values* for O(1) verification (many service
        # names may in principle share a token; we only care that it is known).
        self._values = frozenset(self._tokens.values())

    # --- construction -----------------------------------------------------

    @classmethod
    def from_caller_auth_config(
        cls, cfg: "CallerAuthConfig"
    ) -> "ServiceTokenRegistry":
        """Build from the typed :class:`CallerAuthConfig` (inline JSON / file path)."""
        return cls._from_raw(cfg.service_tokens_json, cfg.service_tokens_file)

    @classmethod
    def _from_raw(
        cls, raw_json: str | None, file_path: str | None
    ) -> "ServiceTokenRegistry":
        """Shared loader: inline JSON wins over a file path.

        Missing config → an empty registry (open, dev default). Malformed config is
        a hard error (LAW 4: never silently ignore) so a bad deploy fails loudly
        instead of silently exposing the API.
        """
        raw = raw_json
        if not raw and file_path:
            raw = Path(file_path).read_text(encoding="utf-8")
        if not raw:
            return cls(tokens={})
        try:
            cfg = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid service-tokens config JSON: {exc}") from exc
        if not isinstance(cfg, dict):
            raise ValueError(
                f"service-tokens config must be a JSON object, got {type(cfg).__name__}"
            )
        return cls(tokens={str(k): str(v) for k, v in cfg.items()})

    # --- resolution -------------------------------------------------------

    def is_open(self) -> bool:
        """True when no token is configured (every call allowed — dev default)."""
        return not self._tokens

    def verify(self, token: str | None) -> bool:
        """True when ``token`` matches a configured service token.

        An open registry (no tokens) accepts everything; otherwise a missing or
        unknown token is rejected.
        """
        if self.is_open():
            return True
        return token is not None and token in self._values


__all__ = ["ServiceTokenRegistry"]
