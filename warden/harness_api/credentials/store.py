"""pre-03 M0 · 3b — the credential store: ``(user_id, provider) → CredentialRecord``.

The multi-tenant storage the auth module resolves against. Keyed by
``(user_id, provider)`` so a single user holding a Claude OAuth token AND a Codex
key is native — the AUTH-4 fix (``KeyRegistry`` mapped ``user → ONE key_id`` and a
provider mismatch silently inherited the process credential).

A :class:`CredentialRecord` holds only REFERENCES (secret env-var names / a session
path), never secret values — the by-reference model of :mod:`.methods`. Two backends
implement the :class:`CredentialStore` Protocol: :class:`JsonlCredentialStore` (the
durable default, mirroring ``governance/jsonl_ledger.py`` — append-only, replayed on
:meth:`load`, single-writer lock, one bad tail line skipped) and
:class:`InMemoryCredentialStore` (the ephemeral tier, for tests + the ``memory``
backend). A Postgres backend is a later injected implementation, like the ledger.

The flat ``MANAGED_KEYS_JSON`` blob still works: :func:`legacy_records_from_config`
translates it into ``(user, provider)`` records that seed a store — so no existing
deploy breaks, and an empty store means "inherit the process credential".
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from warden.harness_api.credentials.methods import (
    ApiKey,
    AuthMethod,
    OAuthToken,
    SessionFile,
)
from warden.providers.auth import PROVIDER_AUTH_VARS

logger = logging.getLogger(__name__)

AuthMethodName = Literal["oauth", "api_key", "session_file"]

# Default home-dir env var for a session-file credential (only codex uses one).
_DEFAULT_HOME_VAR = "CODEX_HOME"


def _default_var(provider: str, auth_method: AuthMethodName) -> str:
    """The injection var when a record does not name its own.

    Derived from the provider's ordered ``PROVIDER_AUTH_VARS`` — the FIRST entry is
    the OAuth var (``CLAUDE_CODE_OAUTH_TOKEN``), the LAST is the API-key var
    (``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY``) — so there is one source of truth
    for auth var names, shared with the injector's strip list.
    """
    order = PROVIDER_AUTH_VARS.get(provider, ())
    if auth_method == "session_file":
        return _DEFAULT_HOME_VAR
    if not order:
        return ""
    return order[0] if auth_method == "oauth" else order[-1]


@dataclass(frozen=True)
class CredentialRecord:
    """One stored credential for a ``(user_id, provider)``, by reference only.

    ``secret_ref`` is the env-var NAME holding the token/key (oauth / api_key) or the
    session directory / its env ref (session_file). ``var`` overrides the injection
    var (the token/key env var, or the session ``home_var``); when unset it defaults
    from the provider's auth-var precedence. ``tier`` is carried for billing parity
    with the legacy registry; ``ts`` orders appends.
    """

    user_id: str
    provider: str
    auth_method: AuthMethodName
    secret_ref: str
    var: str | None = None
    tier: str | None = None
    ts: float = 0.0

    def to_method(self) -> AuthMethod:
        """Project the record to its typed :data:`AuthMethod` (refs only)."""
        var = self.var or _default_var(self.provider, self.auth_method)
        if self.auth_method == "oauth":
            return OAuthToken(var=var, token_ref=self.secret_ref)
        if self.auth_method == "api_key":
            return ApiKey(var=var, key_ref=self.secret_ref)
        if self.auth_method == "session_file":
            return SessionFile(home_var=var, path_ref=self.secret_ref)
        raise ValueError(f"unknown auth_method {self.auth_method!r}")  # LAW 4

    def key(self) -> tuple[str, str]:
        return (self.user_id, self.provider)


@runtime_checkable
class CredentialStore(Protocol):
    """The store contract the :class:`AuthResolver` (3c) reads against."""

    async def load(self) -> None: ...
    def get(self, user_id: str, provider: str) -> CredentialRecord | None: ...
    async def put(self, record: CredentialRecord) -> None: ...
    async def remove(self, user_id: str, provider: str) -> None: ...


def _record_from_dict(data: dict) -> CredentialRecord:
    return CredentialRecord(
        user_id=data["user_id"],
        provider=data["provider"],
        auth_method=data["auth_method"],
        secret_ref=data["secret_ref"],
        var=data.get("var"),
        tier=data.get("tier"),
        ts=data.get("ts", 0.0),
    )


class InMemoryCredentialStore:
    """Ephemeral ``(user, provider) → record`` store (the ``memory`` backend tier).

    No durability — used for tests and single-process runs seeded at startup (e.g.
    from a legacy ``MANAGED_KEYS`` blob). Same read/write surface as the JSONL store.
    """

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], CredentialRecord] = {}

    async def load(self) -> None:
        """No-op (nothing durable to replay); present for Protocol parity."""
        return None

    def get(self, user_id: str, provider: str) -> CredentialRecord | None:
        return self._records.get((user_id, provider))

    async def put(self, record: CredentialRecord) -> None:
        self._records[record.key()] = record

    async def remove(self, user_id: str, provider: str) -> None:
        self._records.pop((user_id, provider), None)

    def seed(self, records: list[CredentialRecord]) -> None:
        """Bulk-load records synchronously (startup seeding, e.g. legacy blob)."""
        for record in records:
            self._records[record.key()] = record


class JsonlCredentialStore:
    """Durable ``(user, provider) → record`` store backed by an append-only JSONL
    file (mirrors :class:`~warden.harness_api.governance.jsonl_ledger.JsonlBalanceLedger`).

    A ``put`` appends a ``put`` event and a later put for the same key wins; a
    ``remove`` appends a ``remove`` event that deletes the key. The in-memory map is
    a fold replayed on :meth:`load`; the file is never rewritten, so a crash can only
    lose the last partial line. A single :class:`asyncio.Lock` serializes writers.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._records: dict[tuple[str, str], CredentialRecord] = {}

    async def load(self) -> None:
        """Replay the JSONL file, folding events into memory (idempotent).

        Missing file ⇒ an empty store. A corrupt/partial last line is logged and
        skipped (LAW 4: one bad tail line must not crash the store); prior events fold.
        """
        async with self._lock:
            self._records = {}
            if not self._path.exists():
                return
            with self._path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except (ValueError, json.JSONDecodeError):
                        logger.warning(
                            "JsonlCredentialStore: skipping corrupt line in %s: %r",
                            self._path,
                            line[:200],
                        )
                        continue
                    self._apply(event)

    def _apply(self, event: dict) -> None:
        """Fold one already-parsed event into the in-memory map."""
        etype = event.get("type")
        user_id = event.get("user_id")
        provider = event.get("provider")
        if user_id is None or provider is None:
            return
        if etype == "put":
            try:
                self._records[(user_id, provider)] = _record_from_dict(event)
            except KeyError:
                logger.warning(
                    "JsonlCredentialStore: skipping put missing fields: %r", event
                )
        elif etype == "remove":
            self._records.pop((user_id, provider), None)
        # Unknown event types are ignored (forward-compat).

    def _append(self, event: dict) -> None:
        """Append one event as a JSON line, creating parent dirs on first write."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")

    def get(self, user_id: str, provider: str) -> CredentialRecord | None:
        """The current record for a ``(user, provider)``, or ``None`` (sync read)."""
        return self._records.get((user_id, provider))

    async def put(self, record: CredentialRecord) -> None:
        """Store (or overwrite) a credential record. Append + fold under the lock."""
        async with self._lock:
            event = {"type": "put", **asdict(record)}
            if not event.get("ts"):
                event["ts"] = time.time()
            self._append(event)
            self._apply(event)

    async def remove(self, user_id: str, provider: str) -> None:
        """Delete a ``(user, provider)`` record (no-op if absent). Append + fold."""
        async with self._lock:
            event = {
                "type": "remove",
                "user_id": user_id,
                "provider": provider,
                "ts": time.time(),
            }
            self._append(event)
            self._apply(event)


def legacy_records_from_config(cfg: Mapping) -> list[CredentialRecord]:
    """Translate a flat ``MANAGED_KEYS`` blob into ``(user, provider)`` records.

    Each ``user → key_id`` mapping becomes ONE api-key record for the key's provider
    (managed keys are operator-provisioned secrets injected under ``auth_var``). The
    ``auth_var`` is preserved as the injection ``var`` — so a key whose var is an
    OAuth var still injects under it. ``default_key_id`` is NOT materialized per
    stranger (the store keys explicit users); a stranger falls through to Inherit or
    the resolver's configured default. Same config shape as ``KeyRegistry.from_config``.
    """
    keys = cfg.get("keys") or {}
    records: list[CredentialRecord] = []
    for user_id, entry in (cfg.get("users") or {}).items():
        key_id = entry.get("key_id")
        key = keys.get(key_id) if key_id else None
        if key is None:
            continue
        provider = key["provider"]
        records.append(CredentialRecord(
            user_id=user_id,
            provider=provider,
            auth_method="api_key",
            secret_ref=key["secret_env"],
            var=key.get("auth_var") or _default_var(provider, "api_key"),
            tier=key.get("tier"),
        ))
    return records


__all__ = [
    "CredentialRecord",
    "CredentialStore",
    "InMemoryCredentialStore",
    "JsonlCredentialStore",
    "legacy_records_from_config",
]
