"""EXT-T1 — caller authentication + per-user authorization on the Runs API.

Two layers, kept distinct (E1 §3):
  * **T1a authentication** (``x-service-token``) — which backend is calling?
  * **T1b authorization** (``x-user-id`` vs the run's owner) — may it touch *this*
    run? A valid token can never reach another user's run.

Deterministic (mock skill, no LLM), following the repo's ``asyncio.run(_run())``
idiom. Authorization is enforced only when the token registry is configured
(non-open), symmetric with authentication's "empty registry ⇒ open".
"""

import asyncio
import tempfile
from pathlib import Path

import httpx
import pytest

from warden.harness_api.app import create_app
from warden.harness_api.config import GovernanceConfig, HarnessApiConfig
from warden.harness_api.credentials.keys import KeyRegistry
from warden.harness_api.credentials.service_tokens import (
    ServiceTokenRegistry,
)
from warden.harness_api.event_log import RunEventLog
from warden.harness_api.runner import Runner
from warden.harness_api.schemas import RunSpec, Sink
from warden.tests.harness_api.mock_skill import Tracker, build_factory


# --- helpers --------------------------------------------------------------

_KEYS = KeyRegistry.from_config(
    {
        "keys": {"k": {"provider": "claude", "secret_env": "S"}},
        "users": {
            "user_a": {"key_id": "k"},
            "user_b": {"key_id": "k"},
        },
    },
    secrets={"S": "sk"},
)


def _tmp_event_log() -> RunEventLog:
    return RunEventLog(Path(tempfile.mkdtemp()) / "run_events.db")


def _make_app(token_registry: ServiceTokenRegistry | None):
    tracker = Tracker()
    runner = Runner(
        HarnessApiConfig(governance=GovernanceConfig(enabled=False)),
        keys=_KEYS,
        chat_api_factory=build_factory(tracker),
        event_log=_tmp_event_log(),
    )
    return create_app(runner, token_registry=token_registry), runner


def _spec(user, task):
    return RunSpec(
        user_id=user, task_id=task, input={"prompt": "hi"}, sink=Sink(type="sse")
    )


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


async def _submit_and_drain(runner, user, task):
    run_id = runner.submit(_spec(user, task))
    await runner.task_for(run_id)
    return run_id


# --- T1a authentication ---------------------------------------------------


def test_service_token_required_when_configured():
    async def _run():
        app, _ = _make_app(ServiceTokenRegistry({"svc": "tok"}))
        async with _client(app) as c:
            body = {"user_id": "user_a", "task_id": "t", "input": {"prompt": "hi"},
                    "sink": {"type": "sse"}}
            # No token → 401.
            assert (await c.post("/runs", json=body)).status_code == 401
            # Wrong token → 401.
            assert (await c.post(
                "/runs", json=body, headers={"x-service-token": "nope"}
            )).status_code == 401
            # Right token → 202.
            assert (await c.post(
                "/runs", json=body, headers={"x-service-token": "tok"}
            )).status_code == 202

    asyncio.run(_run())


def test_empty_registry_is_open():
    async def _run():
        app, _ = _make_app(ServiceTokenRegistry({}))  # empty ⇒ open
        async with _client(app) as c:
            resp = await c.post(
                "/runs",
                json={"user_id": "user_a", "task_id": "t",
                      "input": {"prompt": "hi"}, "sink": {"type": "sse"}},
            )
            assert resp.status_code == 202  # no token needed

    asyncio.run(_run())


def test_health_is_exempt_from_service_token():
    async def _run():
        # A CONFIGURED (non-open) registry must still let /health through with no
        # token — a liveness probe (compose/stack wait_for, LB) carries none.
        app, _ = _make_app(ServiceTokenRegistry({"svc": "tok"}))
        async with _client(app) as c:
            resp = await c.get("/health")  # no x-service-token
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok"}

    asyncio.run(_run())


def test_malformed_service_tokens_json_is_hard_error():
    with pytest.raises(ValueError, match="invalid service-tokens config JSON"):
        ServiceTokenRegistry._from_raw("{not json", None)


def test_service_tokens_config_must_be_object():
    with pytest.raises(ValueError, match="must be a JSON object"):
        ServiceTokenRegistry._from_raw('["a", "b"]', None)


# --- T1b authorization ----------------------------------------------------

_RUN_ROUTES = [
    ("GET", "/runs/{id}"),
    ("GET", "/runs/{id}/events"),
    ("GET", "/runs/{id}/history"),
    ("GET", "/runs/{id}/file?path=out.txt"),
    ("POST", "/runs/{id}/cancel"),
    ("POST", "/runs/{id}/tool_confirmation"),
]


async def _hit(client, method, path, *, token, user):
    headers = {"x-service-token": token, "x-user-id": user}
    if method == "GET":
        return await client.get(path, headers=headers)
    body = {"tool_use_id": "x", "decision": "approve"} if path.endswith(
        "tool_confirmation"
    ) else {}
    return await client.post(path, json=body, headers=headers)


def test_owner_allowed_other_user_forbidden():
    async def _run():
        app, runner = _make_app(ServiceTokenRegistry({"svc": "tok"}))
        run_id = await _submit_and_drain(runner, "user_a", "course")
        async with _client(app) as c:
            for method, tmpl in _RUN_ROUTES:
                path = tmpl.format(id=run_id)
                # Owner (user_a) → NOT 403/404 (the ownership gate passes; the
                # route may still 404 for its own reasons, e.g. cancel a done run).
                owner_resp = await _hit(c, method, path, token="tok", user="user_a")
                assert owner_resp.status_code not in (401, 403), (
                    f"owner blocked on {method} {tmpl}: {owner_resp.status_code}"
                )
                # Wrong user (user_b) with the SAME valid token → 403.
                other = await _hit(c, method, path, token="tok", user="user_b")
                assert other.status_code == 403, (
                    f"expected 403 for wrong user on {method} {tmpl}, "
                    f"got {other.status_code}"
                )

    asyncio.run(_run())


def test_unknown_run_is_404_not_403():
    async def _run():
        app, _ = _make_app(ServiceTokenRegistry({"svc": "tok"}))
        async with _client(app) as c:
            resp = await _hit(
                c, "GET", "/runs/does-not-exist", token="tok", user="user_a"
            )
            assert resp.status_code == 404

    asyncio.run(_run())


def test_open_registry_skips_ownership_check():
    """Single-tenant dev (open registry) does NOT enforce ownership — a request
    with no x-user-id still reaches its own run (backward-compat)."""
    async def _run():
        app, runner = _make_app(ServiceTokenRegistry({}))  # open
        run_id = await _submit_and_drain(runner, "user_a", "course")
        async with _client(app) as c:
            resp = await c.get(f"/runs/{run_id}")  # no x-user-id header
            assert resp.status_code == 200

    asyncio.run(_run())


def test_ownership_survives_restart_via_owner_of_seam():
    """The ownership check resolves the owner through ``runner.owner_of`` — the seam
    E5's durable registry extends. Proven here on the in-memory hit; E5 adds the
    post-restart durable resolution."""
    async def _run():
        app, runner = _make_app(ServiceTokenRegistry({"svc": "tok"}))
        run_id = await _submit_and_drain(runner, "user_a", "course")
        assert await runner.owner_of(run_id) == "user_a"
        assert await runner.owner_of("nope") is None

    asyncio.run(_run())
