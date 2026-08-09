from warden.schemas.providers import AgentProvider

# Providers that cannot yet CONSUME custom tools. Passing non-empty custom_tools
# to one of these must raise (TOOL-1: consume-or-error, never silently drop).
# Claude SDK + OpenHarness consume them (in-proc list). Codex is NOT in this set:
# it delivers custom tools via an in-proc MCP server, but UNGATED and behind an
# explicit `allow_ungated_custom_tools` opt-in — so the CodexSdkSession adapter
# itself decides (raise when the opt-in is off, the fail-closed default).
_CUSTOM_TOOLS_UNSUPPORTED: set[str] = set()


def create_session(provider: str = "claude", **kwargs) -> AgentProvider:
    """Factory to instantiate the correct provider session.

    Session ID is NOT generated here — it will be captured from the
    provider's first streaming message (Claude SDK session_id or Codex thread_id).
    If resuming, resume_session_id is passed through and used as session_id.

    N2: session_id is NO LONGER stripped — it flows through to the provider so a
    caller can pin a deterministic id (resume determinism). Every provider now
    accepts an explicit session_id param + rejects unknown kwargs, so this is safe.

    C1 interim guard: providers that do not yet consume custom tools raise on a
    non-empty custom_tools rather than silently dropping it (Claude SDK is exempt
    — it consumes them).
    """
    if provider in _CUSTOM_TOOLS_UNSUPPORTED and kwargs.get("custom_tools"):
        raise NotImplementedError(
            f"{provider} does not support custom_tools yet"
        )
    # --- Retired adapters (mv-only gate — files kept, key gated) --------------
    # D6: `codex exec` is subsumed by the Codex Python SDK adapter. The canonical
    # `codex` key now routes to CodexSdkSession; the old exec CodexSession file is
    # kept but reachable only under the legacy `codex-exec` key, which is gated.
    if provider == "codex-exec":
        raise NotImplementedError(
            "codex exec retired — use the codex SDK adapter (provider='codex', D6)"
        )
    # D7: `claude -p` (the CLI adapter) is retired in favor of the Claude SDK.
    if provider == "claude-cli":
        raise NotImplementedError(
            "claude -p retired — use the claude SDK (provider='claude', D7)"
        )

    return _session_class(provider)(**kwargs)


def _session_class(provider: str) -> type[AgentProvider]:
    """Resolve a provider NAME to its session CLASS (lazy import, no instance).

    The single name→class routing table, shared by :func:`create_session` (which
    instantiates) and :func:`provider_capability` (which reads a class attribute
    WITHOUT instantiating — the Runner needs the ``hard_kill_tier`` /
    ``max_output_tokens`` capability flags before any session exists).
    """
    if provider == "codex":
        # Subsume: the canonical codex provider is the SDK adapter (handler always
        # on, fail-closed exec/patch gating). The old exec CodexSession is kept
        # (mv-only) under the gated `codex-exec` key above.
        from warden.providers.codex.sdk_session import CodexSdkSession
        return CodexSdkSession
    elif provider == "openharness":
        from warden.providers.openharness.session import OpenHarnessSession
        return OpenHarnessSession
    else:
        from warden.providers.claude.session import ClaudeSession
        return ClaudeSession


def provider_capability(provider: str, flag: str, default=None):
    """Read a capability CLASS attribute off a provider without instantiating it.

    Mirrors :func:`create_session`'s name→class routing (lazy imports) so the
    Runner can consult a provider's ``hard_kill_tier`` (deadline guard) or
    ``max_output_tokens`` (worst-case reservation) before a session is built.
    """
    return getattr(_session_class(provider), flag, default)


def provider_hard_kill_tier(provider: str) -> str:
    """The provider's deadline-enforcement tier ("os"|"cooperative"|"none")."""
    return provider_capability(provider, "hard_kill_tier", "none")


def provider_max_output_tokens(provider: str) -> int | None:
    """The provider's native max output-token bound (None ⇒ provider-managed)."""
    return provider_capability(provider, "max_output_tokens", None)
