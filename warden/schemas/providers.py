from collections.abc import AsyncGenerator
from typing import Any, Literal, Protocol, runtime_checkable

# --- Capability-flag literal aliases (declared read-only Protocol members) ---
# These are provider-DECLARED facts the Governor / compaction dispatch reads
# (never isinstance). See warden/docs/improve_scope/tasks/
# provider-finalization/phase-1-design-coverage.md §4 for the authoritative
# per-provider values.
HardKillTier = Literal["os", "cooperative", "none"]
CostVisibility = Literal["mid_turn", "coarse", "terminal"]
Compaction = Literal["native", "harness_driven"]
CustomToolDelivery = Literal["in_proc_list", "mcp", "none"]
PermTier = Literal["arg_level", "name_level", "none"]
# C4 — who owns retrying a transient transport error: the provider's own SDK
# (``sdk``), the harness/Governor via backoff (``harness``), or nobody (``none``,
# deliberate fail-fast). Read by the Governor so retries never stack on SDK ones.
RetryOwner = Literal["sdk", "harness", "none"]


@runtime_checkable
class AgentProvider(Protocol):
    """Base protocol for all agent providers (Claude, Codex, etc.).

    A provider is a pure transport/mechanism around one agent SDK/CLI: it owns
    the session lifecycle (``start``/``send``/``stop``/``close``), the captured
    ``session_id``, the per-run transcript ``jsonl_path``, and it DECLARES its
    capabilities via the seven read-only flag properties below so the Governor
    and compaction paths dispatch on a declared fact rather than an
    ``isinstance`` check or a runtime surprise.

    Explicitly OUT of provider scope (orchestrator / core concerns — no provider
    grows these responsibilities):
      * **SESS-3 (snapshot-survives-failed-turn)** — session-state persistence
        and restore is the orchestrator's job (snapshot after turn / restore at
        init), NOT a provider member.
      * **SAFE-1 (output-filter core pass)** — the first-class output filter runs
        on the drain side of the orchestrator's per-turn queue, NOT inside a
        provider. A provider surfaces its raw stream; it never filters it.
    """

    session_id: str
    jsonl_path: str | None

    # --- Capability flags (read-only; declared per provider) -----------------
    @property
    def crash_isolated(self) -> bool: ...
    @property
    def hard_kill_tier(self) -> HardKillTier: ...
    @property
    def cost_visibility(self) -> CostVisibility: ...
    @property
    def compaction(self) -> Compaction: ...
    @property
    def supports_hard_deadline(self) -> bool: ...
    @property
    def custom_tool_delivery(self) -> CustomToolDelivery: ...
    @property
    def perm_tier(self) -> PermTier: ...
    @property
    def retry_owner(self) -> RetryOwner: ...  # C4
    # C6 — the harness-enforced max output tokens per turn, exposed for
    # ``compaction: harness_driven`` providers (Ollama) where the harness owns the
    # window; ``None`` when the provider/SDK manages it natively (Claude/Codex).
    @property
    def max_output_tokens(self) -> int | None: ...

    # --- Lifecycle -----------------------------------------------------------
    async def start(self) -> None: ...
    async def send(self, prompt: str) -> AsyncGenerator[Any, None]: ...
    async def stop(self) -> None: ...
    async def close(self) -> None: ...
