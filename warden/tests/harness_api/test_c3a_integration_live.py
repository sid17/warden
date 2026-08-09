"""EXT-C3a integration gate — a full run end-to-end on the POSTGRES backend.

The proceed/no-proceed gate for C3a: with the ``state.backend`` tier switch flipped to
``postgres``, the real ``Runner`` must run a full run (create → events → status →
resume) entirely on the shared Postgres stores — and a SECOND ``Runner`` (a simulated
replica / restart) sharing the same DSN must resolve identity + reconstruct state for a
run it never held in memory. That cross-instance recovery is the whole multi-replica
premise; here it is proven in-process against a live Postgres (the Docker bed adds the
real two-container proof).

Opt-in: skipped unless ``WARDEN_TEST_POSTGRES_DSN`` is set (the default suite stays
DB-free). Run:

    WARDEN_TEST_POSTGRES_DSN=postgresql://warden:warden@localhost:5432/warden_test \\
      uv run --no-sync python -m pytest \\
      warden/tests/harness_api/test_c3a_integration_live.py -q
"""

from __future__ import annotations

import asyncio
import os

import pytest

from warden.harness_api.config import (
    GovernanceConfig,
    HarnessApiConfig,
    StateBackendConfig,
)
from warden.harness_api.credentials.keys import KeyRegistry
from warden.harness_api.schemas import RunSpec, Sink
from warden.tests.harness_api.mock_skill import Tracker, build_factory

_DSN = os.environ.get("WARDEN_TEST_POSTGRES_DSN")

pytestmark = pytest.mark.skipif(
    not _DSN, reason="set WARDEN_TEST_POSTGRES_DSN to run the C3a integration gate"
)

_KEYS = KeyRegistry.from_config(
    {"keys": {"k": {"provider": "claude", "secret_env": "S"}},
     "users": {"u1": {"key_id": "k"}}},
    secrets={"S": "sk"},
)


def _pg_cfg() -> HarnessApiConfig:
    """All shared stores on Postgres via the ONE tier switch (governance off — this
    gate proves the state backends, not the Governor)."""
    return HarnessApiConfig(
        governance=GovernanceConfig(enabled=False),
        state=StateBackendConfig(backend="postgres", dsn=_DSN),
    )


def _spec(user="u1", task="c3a-course", **kw) -> RunSpec:
    return RunSpec(user_id=user, task_id=task, input={"prompt": "hi"},
                   sink=Sink(type="sse"), **kw)


async def _cleanup(dsn: str, run_id: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("DELETE FROM run_events WHERE run_id = $1", run_id)
        await conn.execute("DELETE FROM run_identities WHERE run_id = $1", run_id)
    finally:
        await conn.close()


def test_c3a_full_run_and_cross_replica_recovery_on_postgres():
    """One Runner (replica A) runs a full run on Postgres; a fresh Runner (replica B),
    sharing only the DSN, resolves the owner from the shared registry and reconstructs
    the succeeded view from the shared event log — the run it never held in memory."""
    async def _run():
        from warden.harness_api.runner import Runner

        # --- replica A: build the run on the shared Postgres stores ---
        a = Runner(
            _pg_cfg(),
            keys=_KEYS,
            chat_api_factory=build_factory(
                Tracker(), usage={"input_tokens": 10, "output_tokens": 20}
            ),
        )
        await a.init()
        run_id = a.submit(_spec())
        try:
            await a.task_for(run_id)
            live = a.get(run_id)
            assert live.status == "succeeded"          # ran end-to-end
            assert await a.owner_of(run_id) == "u1"    # identity in shared registry
            await a.aclose()

            # --- replica B: a fresh process sharing ONLY the Postgres DSN ---
            b = Runner(
                _pg_cfg(),
                keys=_KEYS,
                chat_api_factory=build_factory(Tracker()),
            )
            await b.init()
            assert b.get(run_id) is None               # never in B's memory
            # identity resolves from the SHARED Postgres registry (live read)...
            assert await b.owner_of(run_id) == "u1"
            # ...and the full view is reconstructed from the SHARED Postgres event log.
            view = await b.get_durable(run_id)
            assert view is not None
            assert view.run_id == run_id
            assert view.status == "succeeded"
            assert view.usage.get("input") == 10
            assert view.usage.get("output") == 20
            await b.aclose()
        finally:
            await _cleanup(_DSN, run_id)

    asyncio.run(_run())


def test_c3a_unknown_run_is_none_on_postgres():
    """A run_id unknown to the shared registry AND event log → None (the 404 path)."""
    async def _run():
        from warden.harness_api.runner import Runner

        r = Runner(_pg_cfg(), keys=_KEYS, chat_api_factory=build_factory(Tracker()))
        await r.init()
        assert await r.get_durable("c3a-nope-does-not-exist") is None
        assert await r.owner_of("c3a-nope-does-not-exist") is None
        await r.aclose()

    asyncio.run(_run())
