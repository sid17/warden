"""pre-03 M0 · 3e — the auth switchboard: build an ``AuthResolver`` from Settings.

Mirrors ``governance/config.py`` (``build_governor_service``): every credential knob
is config-driven, assembled from the typed
:class:`~warden.harness_api.config.HarnessApiConfig`, never read off
``os.environ`` (secret *values* are the only env, by design). :func:`build_auth_resolver`
is the single construction point the Runner calls; the Governor is handed the SAME
instance so governed and ungoverned runs resolve credentials one way (pre-03 3d).

Two store backends:

  * ``memory`` (default) — seeds the legacy ``MANAGED_KEYS`` blob in-process via the
    :class:`~warden.harness_api.credentials.keys.KeyRegistry` legacy adapter.
    Preserves current single/managed behavior exactly, now with a configurable policy.
  * ``jsonl`` — the durable append-only per-``(user, provider)`` store
    (``credentials.jsonl`` in the state dir); records are seeded externally (the Docker
    bed / an admin path) and replayed by :func:`init_auth` at startup. The operator's
    ``default_key_id`` still serves as the per-provider fallback.
"""

from __future__ import annotations

from pathlib import Path

from warden.harness_api.config import HarnessApiConfig
from warden.harness_api.credentials.keys import KeyRegistry
from warden.harness_api.credentials.resolver import AuthPolicy, AuthResolver
from warden.harness_api.credentials.store import JsonlCredentialStore


def _auth_state_dir(cfg: HarnessApiConfig) -> Path:
    """Where ``credentials.jsonl`` lives — ``cfg.auth.state_dir`` when set, else next
    to the engine's session DB (one control-plane state dir, beside ``balance.jsonl``),
    falling back to ``data/`` (mirrors ``governance/config._state_dir``)."""
    if cfg.auth.state_dir:
        return Path(cfg.auth.state_dir)
    session_db = getattr(cfg.engine.persistence, "session_db_path", None)
    return Path(session_db).parent if session_db else Path("data")


def _policy_from_cfg(cfg: HarnessApiConfig) -> AuthPolicy:
    allowed = cfg.auth.oauth_allowed_users
    return AuthPolicy(
        oauth_allowed_users="*" if allowed == "*" else tuple(allowed),
        on_oauth_denied=cfg.auth.on_oauth_denied,
    )


def build_auth_resolver(cfg: HarnessApiConfig) -> AuthResolver:
    """Construct the :class:`AuthResolver` from the typed config (see module docstring).

    The JSONL-backed store is UNLOADED; the caller runs :func:`init_auth` at startup to
    replay ``credentials.jsonl``. The ``memory`` backend needs no init.
    """
    policy = _policy_from_cfg(cfg)
    # The legacy adapter gives us both the memory-seeded resolver AND the operator's
    # default-key fallback — reused (LAW 2) rather than re-deriving from the blob.
    legacy = KeyRegistry.from_keys_config(cfg.keys).to_auth_resolver(policy=policy)
    if cfg.auth.store_backend != "jsonl":
        return legacy

    store = JsonlCredentialStore(_auth_state_dir(cfg) / "credentials.jsonl")
    return AuthResolver(
        store=store,
        policy=policy,
        fallback_methods=legacy.fallback_methods,
    )


async def init_auth(resolver: AuthResolver) -> None:
    """Replay the durable credential store into memory (call once at startup;
    idempotent). No-op for the ``memory`` backend."""
    store = getattr(resolver, "store", None)
    if isinstance(store, JsonlCredentialStore):
        await store.load()


__all__ = ["build_auth_resolver", "init_auth"]
