"""BaseProvider — shared mechanism mixin for every agent provider.

This is the standing guardrail + shared-helper base that all three providers
(Claude SDK, Codex, OpenHarness) subclass. It closes the silent-drop class that
was the shared root of N2/N3/B8/B15/C1: any capability a caller passes must be an
EXPLICIT typed ``__init__`` param on the provider — anything left over lands in
``**kwargs`` and ``_reject_unknown_kwargs`` turns it into a hard error instead of
a silent no-op.

Scope (Phase 1): reject-unknown-kwargs guardrail, the strip-then-inject auth
helper (promoted from the proven ``cli_session.py`` pattern — the one transport
that already did per-run auth right), and default no-op override seams for
``describe_auth`` / ``install_hooks`` / event normalization. Providers override
the divergent seams; this base never forces a uniform body.
"""

from __future__ import annotations

from typing import Any


class BaseProvider:
    """Shared mechanism base for agent providers (see module docstring)."""

    # --- Guardrail: reject-unknown-kwargs ------------------------------------
    def _reject_unknown_kwargs(self, kwargs: dict[str, Any]) -> None:
        """Raise if any residual ``**kwargs`` remain after the provider has bound
        every typed input it understands.

        THE standing guardrail. Every provider ``__init__`` calls this with its
        leftover ``**kwargs`` so a mistyped/unsupported capability surfaces as a
        ``TypeError`` at construction rather than being silently swallowed.
        """
        if kwargs:
            raise TypeError(
                f"{type(self).__name__} received unknown kwargs: {sorted(kwargs)}"
            )

    # --- Shared helper: strip-then-inject per-run auth ------------------------
    @staticmethod
    def apply_auth_env(
        env: dict[str, str],
        provider_key: str,
        auth_env: dict[str, str] | None,
    ) -> dict[str, str]:
        """Strip every inherited credential for ``provider_key`` from ``env`` then
        overlay ``auth_env`` — the proven per-run key-isolation pattern promoted
        from ``providers/claude/cli_session.py`` (the single source of truth).

        Pure with respect to ``os.environ`` (mutates + returns the passed dict
        only, never the process environment). No-op when ``auth_env is None`` so
        the inherited-credential (single-user) behavior is unchanged.

        Dropping the inherited vars FIRST is load-bearing: an operator's
        ``os.environ`` OAuth token would otherwise shadow an injected API key
        (the transport prefers OAuth over API key), so concurrent runs each
        carrying a different key would bleed.
        """
        if auth_env is None:
            return env
        # Imported here to avoid a module-load cycle (auth imports nothing heavy,
        # but keep the base dependency-light).
        from warden.providers.auth import PROVIDER_AUTH_VARS

        for var in PROVIDER_AUTH_VARS.get(provider_key, ()):
            env.pop(var, None)
        env.update(auth_env)
        return env

    # --- Override seams (default no-op) ---------------------------------------
    def describe_auth(self) -> dict[str, Any]:
        """Report the active auth by TAG/FINGERPRINT, never the raw key (AUTH-3).

        Default no-op override point. Providers with a resolvable credential
        override to return a non-sensitive descriptor (e.g. mode + last-4).
        """
        return {}

    def install_hooks(self, *args: Any, **kwargs: Any) -> None:
        """Generic hook-install seam (refinement 5.1).

        A SINGLE seam that BOTH the audit hook (AUD-1) and the ``PreToolUse``
        sensitive-path hook (SAFE-6) install through — it is deliberately NOT an
        audit-only method, because the path-enforcement hook is a different hook
        that must fire even for auto-allowed tools where ``can_use_tool`` never
        runs. Only providers with a native hook system (Claude SDK, OpenHarness)
        implement it; Codex leaves it a no-op.

        Default no-op. ``ClaudeSession`` overrides it to merge the env-gated v14
        audit hooks into its SDK options (C11 — previously inlined in ``start()``).
        """
        return None

    # --- Event normalization (default: kind-keyed dict -> typed MessageEvent) --
    # The N1 / Phase-4 event vocabulary now EXISTS (schemas/events.py +
    # harness_api/schemas EventType incl. stopped/compaction/tool_result), so this
    # is no longer an identity stub. The default mirrors the normalization the
    # orchestrator already performs at drain time (orchestrator.py) — a provider
    # message handler emits ``{"kind", <content…>, "sessionId"}`` dicts, and this
    # turns one into a typed ``MessageEvent``. Providers may override for native
    # mapping; ``parent_observation_id`` is reserved for OpenHarness cross-process
    # span nesting.
    def normalize_event(self, raw: Any, parent_observation_id: str | None = None) -> Any:
        """Normalize one provider message into the typed ``Event`` union.

        A ``kind``-keyed dict becomes a :class:`~warden.schemas.events.MessageEvent`;
        anything already typed, or a shape without a ``kind``, is returned
        unchanged (fail-soft — a normalizer must never drop a message).
        """
        from warden.schemas.events import MessageEvent

        if isinstance(raw, dict) and "kind" in raw:
            content = {
                k: v for k, v in raw.items()
                if k not in ("kind", "sessionId", "id", "timestamp")
            }
            return MessageEvent(
                kind=raw["kind"],
                content=content,
                session_id=raw.get("sessionId", ""),
            )
        return raw
