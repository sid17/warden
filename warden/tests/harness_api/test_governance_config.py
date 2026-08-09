"""M2 3g.2a — the Governor switchboard: build a GovernorService from config.

Hermetic (no LLM/subprocess/DB). Exercises the factory end to end: disabled ⇒
(None, None) (ungoverned, GOV-2); enabled+jsonl ⇒ a real GovernorService whose
resolve() sees the DURABLE balance from the JSONL file (config → factory → JSONL
ledger → resolve); postgres ⇒ a clear NotImplementedError (no asyncpg import). Also
asserts the Runner is config-driven when no governor_service is injected, and a G1
probe that the factory reads config off the passed ``cfg``, not ``os.environ``.
"""

import asyncio
import tempfile
from pathlib import Path

from warden.harness_api.config import GovernanceConfig, HarnessApiConfig
from warden.harness_api.credentials.keys import KeyRegistry
from warden.harness_api.event_log import RunEventLog
from warden.harness_api.governance import (
    GovernorService,
    build_governor_service,
    init_governance,
)
from warden.harness_api.governance.jsonl_ledger import JsonlBalanceLedger
from warden.harness_api.runner import Runner

_KEYS_JSON = (
    '{"keys": {"k1": {"provider": "claude", "secret_env": "S1"}}, '
    '"users": {"u1": {"key_id": "k1", "budget_usd": 100.0}}}'
)


def _cfg(gov: GovernanceConfig) -> HarnessApiConfig:
    from warden.harness_api.config import KeysConfig

    return HarnessApiConfig(
        keys=KeysConfig(managed_keys_json=_KEYS_JSON), governance=gov
    )


# --- 1. disabled ⇒ (None, None): the ungoverned path (GOV-2) --------------

def test_disabled_returns_none_pair():
    cfg = _cfg(GovernanceConfig(enabled=False))
    service, store = build_governor_service(cfg)
    assert service is None
    assert store is None


# --- 2. enabled + jsonl ⇒ resolve() reads the durable JSONL balance -------

def test_enabled_jsonl_resolve_sees_durable_balance():
    async def _run():
        state = Path(tempfile.mkdtemp())
        cfg = _cfg(
            GovernanceConfig(
                enabled=True, balance_backend="jsonl", state_dir=str(state)
            )
        )
        service, store = build_governor_service(cfg)
        assert isinstance(service, GovernorService)
        assert store is not None

        # Replay the (empty) files, then credit the durable balance file and re-load
        # so resolve() reads the persisted credit — full config→factory→JSONL path.
        await init_governance(service, store)
        backend = service._balance_source
        assert isinstance(backend, JsonlBalanceLedger)
        await backend.credit("u1", 42.0, txn_id="t1")

        rg = await service.resolve(
            user_id="u1", task_id="course_A", provider="claude",
            model="claude-opus-4-8",
        )
        # The RunGovernor's opening balance came from the durable JSONL ledger.
        assert rg.opening_balance_usd == 42.0
        # The managed key was resolved too (auth threaded from config keys).
        assert rg.auth_env == {"ANTHROPIC_API_KEY": "sk-1"}

    import os

    os.environ["S1"] = "sk-1"
    try:
        asyncio.run(_run())
    finally:
        os.environ.pop("S1", None)


# --- 3. postgres ledger ⇒ a clear NotImplementedError (no asyncpg) --------

def test_postgres_ledger_raises_not_implemented():
    cfg = _cfg(GovernanceConfig(enabled=True, ledger_backend="postgres"))
    raised = False
    try:
        build_governor_service(cfg)
    except NotImplementedError as exc:
        raised = True
        assert "PostgresReservationLedger" in str(exc)
    assert raised


# --- 4. Runner is config-driven when no governor_service is injected -------

def _runner(cfg: HarnessApiConfig) -> Runner:
    return Runner(
        cfg,
        keys=KeyRegistry.from_config({"keys": {}, "users": {}}, secrets={}),
        event_log=RunEventLog(Path(tempfile.mkdtemp()) / "run_events.db"),
    )


def test_runner_config_drives_governance_enabled():
    state = Path(tempfile.mkdtemp())
    cfg = _cfg(
        GovernanceConfig(enabled=True, balance_backend="jsonl", state_dir=str(state))
    )
    import os

    os.environ["S1"] = "sk-1"
    try:
        runner = _runner(cfg)
    finally:
        os.environ.pop("S1", None)
    assert runner._governor_service is not None
    assert runner._task_policy_store is not None


def test_runner_ungoverned_when_config_disabled():
    cfg = _cfg(GovernanceConfig(enabled=False))
    runner = _runner(cfg)
    assert runner._governor_service is None
    assert runner._task_policy_store is None


def test_explicit_service_wins_over_config():
    # An injected governor_service is used verbatim; the config switchboard is not
    # consulted (the store stays None).
    from warden.harness_api.governance import (
        InMemoryReservationLedger,
        StaticBalanceSource,
    )

    injected = GovernorService(
        key_registry=KeyRegistry.from_config({"keys": {}, "users": {}}, secrets={}),
        ledger=InMemoryReservationLedger(),
        balance_source=StaticBalanceSource({"u1": 5.0}),
    )
    cfg = _cfg(GovernanceConfig(enabled=True, balance_backend="null"))
    runner = Runner(
        cfg,
        keys=KeyRegistry.from_config({"keys": {}, "users": {}}, secrets={}),
        event_log=RunEventLog(Path(tempfile.mkdtemp()) / "run_events.db"),
        governor_service=injected,
    )
    assert runner._governor_service is injected
    assert runner._task_policy_store is None


# --- 5. G1 probe: the factory reads config, not os.environ ----------------

def test_factory_reads_config_not_environ():
    import ast

    src = Path(
        "warden/harness_api/governance/config.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    # No ``import os`` / ``from os import ...`` in the switchboard, and no name
    # ``os`` referenced — every knob comes off the typed ``cfg`` (a prose mention of
    # ``os.environ`` in the docstring is invisible to the AST, so no false positive).
    imports_os = any(
        (isinstance(n, ast.Import) and any(a.name == "os" for a in n.names))
        or (isinstance(n, ast.ImportFrom) and n.module == "os")
        for n in ast.walk(tree)
    )
    references_os = any(
        isinstance(n, ast.Name) and n.id == "os" for n in ast.walk(tree)
    )
    assert not imports_os
    assert not references_os
    # The module resolves the KeyRegistry via the typed config, not raw env.
    assert "from_keys_config" in src
