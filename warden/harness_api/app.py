"""The Runs API — a thin FastAPI app over the concurrent :class:`Runner`.

Four routes (L1 plan §3). Route handlers stay thin: validate, delegate to the
runner, shape the response. All execution logic lives in ``runner.py``.

    POST /runs                 -> 202 {run_id}   (spawn a background run)
    GET  /runs/{id}/events     -> SSE hold-open  (egress option B)
    GET  /runs/{id}            -> status snapshot
    POST /runs/{id}/cancel     -> cancel a running agent
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from warden.harness_api.config import get_harness_api_config
from warden.harness_api.credentials.service_tokens import (
    ServiceTokenRegistry,
)
from warden.harness_api.runner import Runner
from warden.harness_api.schemas import (
    Event,
    ProvisionAck,
    ProvisionSpec,
    RunAccepted,
    RunSpec,
    RunView,
    SeedAccepted,
    ToolConfirmation,
)


async def require_service_token(
    request: Request,
    x_service_token: str | None = Header(default=None),
) -> None:
    """EXT-T1a authentication — reject a caller with no valid service token.

    App-wide dependency: every internal route requires a known ``x-service-token``.
    An **empty** registry (no tokens configured) is open — the single-tenant dev
    default. This proves *which backend* is calling; the per-run ownership check
    (:func:`require_run_owner`) proves it may touch *this* run.

    ``/health`` is exempt: a liveness probe (compose/stack ``wait_for``, a load
    balancer) carries no service token, so it must answer even when a registry IS
    configured. It exposes no run data, so exempting it leaks nothing.
    """
    if request.url.path == "/health":
        return
    registry: ServiceTokenRegistry = request.app.state.token_registry
    if not registry.verify(x_service_token):
        raise HTTPException(401, "invalid or missing service token")


async def require_run_owner(
    request: Request,
    run_id: str,
    x_user_id: str | None = Header(default=None),
) -> None:
    """EXT-T1b authorization — a caller may only touch its own user's runs.

    The run's owning ``user_id`` (from the in-memory registry, or E5's durable
    ``runs`` table after a restart) must equal the caller-asserted ``x-user-id``,
    else ``403``. An unknown run → ``404`` (not ``403`` — distinguishes "no such
    run" from "wrong user"). ``task_id`` is NOT gated here: the caller is trusted to
    request the right run/task; the only hard harness guarantee is correct-user
    isolation (the read-side enforcement of §8 tenant isolation).

    **Enforced only when caller-auth is configured** (a non-open token registry) —
    symmetric with authentication's "empty registry ⇒ open". A single-tenant dev
    deploy (no service tokens) has one trusted caller and no tenancy to isolate, so
    ownership is not enforced; a multi-tenant deploy that configures service tokens
    gets the full ``403`` isolation guarantee. This matches the T1b test contract,
    which always drives a token'd (configured) request.
    """
    if request.app.state.token_registry.is_open():
        return
    owner = await request.app.state.runner.owner_of(run_id)
    if owner is None:
        raise HTTPException(404, f"unknown run {run_id}")
    if x_user_id != owner:
        raise HTTPException(403, "run does not belong to the asserted user")


def create_app(
    runner: Runner | None = None,
    *,
    token_registry: ServiceTokenRegistry | None = None,
) -> FastAPI:
    """Build the Runs API app around a (possibly injected) runner.

    Every route is behind :func:`require_service_token` (app-wide authentication);
    run-scoped routes add :func:`require_run_owner` (per-user authorization). A
    ``token_registry`` may be injected (tests); else it is built from config.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await app.state.runner.init()  # open the durable run-events log (C2)
        yield
        await app.state.runner.aclose()  # cancel in-flight runs + close the log

    config = get_harness_api_config()
    app = FastAPI(
        title="Harness Runs API",
        lifespan=lifespan,
        # App-wide authentication: a valid service token is necessary on every
        # route (empty registry ⇒ open). Authorization is layered per run-route.
        dependencies=[Depends(require_service_token)],
    )
    app.state.runner = runner or Runner(config)
    app.state.token_registry = token_registry or (
        ServiceTokenRegistry.from_caller_auth_config(config.caller_auth)
    )

    @app.get("/health")
    async def health() -> dict:
        """Unauthenticated liveness probe (for compose/stack ``wait_for``).

        The app-wide ``require_service_token`` dependency no-ops on an open (empty)
        registry and, once a registry IS configured, must not gate liveness — a load
        balancer/orchestrator health check carries no service token. Mirrors the mock
        (``harness_api_mock/app.py``); returns ``{"status": "ok"}`` with a 200.
        """
        return {"status": "ok"}

    @app.post("/runs", status_code=202, response_model=RunAccepted)
    async def start_run(spec: RunSpec) -> RunAccepted:
        # POST /runs is NOT run-scoped — it ESTABLISHES the owner, so it needs only
        # authentication (the app-wide token), not the ownership check.
        if spec.sink.type == "webhook" and not spec.sink.url:
            raise HTTPException(422, "webhook sink requires a url")
        run_id = app.state.runner.submit(spec)
        return RunAccepted(run_id=run_id)

    @app.post("/seeds", status_code=201, response_model=SeedAccepted)
    async def upload_seed(request: Request) -> SeedAccepted:
        """EXT-W1 — upload a reusable seed bundle → an opaque, immutable seed_ref.

        Not run-scoped (seeds are product-global content), so gated only by the
        service token. The bundle is the raw request body (a ``.tar.gz`` of
        ``skills/``, ``agents/``, ``workflows/``)."""
        bundle = await request.body()
        if not bundle:
            raise HTTPException(422, "empty seed bundle")
        ref = await app.state.runner.store_seed(bundle)
        return SeedAccepted(seed_ref=ref)

    @app.post("/provision", response_model=ProvisionAck)
    async def provision(
        spec: ProvisionSpec,
        request: Request,
        x_user_id: str | None = Header(default=None),
    ) -> ProvisionAck:
        """EXT-W1 — lay a seed into a ``(user, task)`` workspace, return the ack.

        Provisioning writes into a user's box, so — when caller-auth is configured —
        the caller-asserted ``x-user-id`` must match ``spec.user_id`` (the same
        correct-user guarantee the run-scoped routes give; ``task_id`` is the caller's
        responsibility). Idempotent; a failed lay-down surfaces as an HTTP error."""
        if not request.app.state.token_registry.is_open() and x_user_id != spec.user_id:
            raise HTTPException(403, "asserted user_id does not match provision target")
        try:
            ack = await app.state.runner.provision(
                spec.user_id, spec.task_id, spec.seed_ref
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except RuntimeError as exc:
            raise HTTPException(500, str(exc))
        return ProvisionAck(**ack)

    @app.get(
        "/runs/{run_id}",
        response_model=RunView,
        dependencies=[Depends(require_run_owner)],
    )
    async def get_run(run_id: str) -> RunView:
        # EXT-C1: durable read — in-memory hit, else identity from the RunRegistry +
        # state reconstructed from the event log (survives a restart).
        view = await app.state.runner.get_durable(run_id)
        if view is None:
            raise HTTPException(404, f"unknown run {run_id}")
        return view

    @app.get("/runs/{run_id}/file", dependencies=[Depends(require_run_owner)])
    async def get_run_file(run_id: str, path: str) -> Response:
        """EXT-A1 — return the bytes of one generated file from the run's snapshot.

        Behind **both** auth layers (the app-wide token + the ownership check): the
        token proves the backend, the ownership check proves the run's ``user_id``
        matches the caller, so a valid token can never read another user's workspace.
        Confined path (``..``/NUL/escape → 400); unknown run or absent archive → 404.
        """
        try:
            data = await app.state.runner.read_run_file(run_id, path)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        return Response(content=data, media_type="application/octet-stream")

    @app.get("/runs/{run_id}/events", dependencies=[Depends(require_run_owner)])
    async def stream_events(run_id: str) -> StreamingResponse:
        """Live SSE buffer — **SINGLE-REPLICA ONLY** (EXT-C3d).

        The SSE stream is an in-memory buffer pinned to the replica that RUNS the run;
        a load balancer that routes this GET to a different replica finds no buffer and
        404s. For a multi-replica deployment the durable, replica-agnostic reconnection
        path is ``GET /runs/{id}/history?after=<seq>`` (reads the shared event log — any
        replica replays the gap with no loss/dup, the log being INSERT-OR-IGNORE on
        ``(run_id, seq)``). External consumers should use webhooks or history-polling;
        SSE is for the single-container / sticky-session case. (A cross-replica live
        fan-out via Redis pub/sub is a deliberate v-next, not built here.)
        """
        sse = app.state.runner.sse_for(run_id)
        if sse is None:
            raise HTTPException(404, f"no sse stream for run {run_id}")

        async def gen() -> AsyncIterator[str]:
            async for event in sse.stream():
                yield f"data: {json.dumps(event.model_dump())}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get(
        "/runs/{run_id}/history",
        response_model=list[Event],
        dependencies=[Depends(require_run_owner)],
    )
    async def run_history(run_id: str, after: int = 0) -> list[Event]:
        """Durable replay (C2): events with ``seq > after``, survives teardown.

        Unlike ``/events`` (the live in-memory SSE buffer), this reads the
        append-only ``run_events`` log, so a reconnecting consumer resumes from its
        last-seen seq even after the process that ran it is gone.
        """
        return await app.state.runner.replay(run_id, after)

    @app.post("/runs/{run_id}/cancel", dependencies=[Depends(require_run_owner)])
    async def cancel_run(run_id: str) -> dict:
        ok = await app.state.runner.cancel(run_id)
        if not ok:
            raise HTTPException(404, f"run {run_id} not cancellable")
        return {"run_id": run_id, "status": "cancelling"}

    @app.post(
        "/runs/{run_id}/tool_confirmation",
        dependencies=[Depends(require_run_owner)],
    )
    async def confirm_tool(run_id: str, body: ToolConfirmation) -> dict:
        """M6/E6 durable HITL resume: record a decision for a paused ask and re-drive.

        E6 three modes — ``approve`` (run the tool), ``reject`` (halt), ``revise``
        (re-plan with ``feedback`` and re-submit). Idempotent on ``(run_id,
        tool_use_id)`` — a duplicate returns the recorded decision without re-running
        the tool. Carries a decision, never a credential.
        """
        result = await app.state.runner.confirm(
            run_id, body.tool_use_id,
            decision=body.decision, reason=body.reason or "",
            feedback=body.feedback,
            updated_input=body.updated_input,  # EXT-G2 dormant (unused by the 3 modes)
        )
        if result is None:
            raise HTTPException(404, f"unknown run {run_id}")
        return result

    return app


app = create_app()
