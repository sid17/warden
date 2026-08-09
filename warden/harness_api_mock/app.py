"""The mock Runs API — a thin FastAPI app over :class:`MockRunner`.

Near-copy of the real ``harness_api/app.py`` (so the future swap is a visible diff)
plus (a) the new ``GET /runs/{id}/file`` route (EXT-A1) and (b) an ``x-service-token``
auth dependency on EVERY route. Route handlers stay thin: validate, delegate to the
runner, shape the response.

    POST /runs                        -> 202 {run_id}
    GET  /runs/{id}                   -> status snapshot
    GET  /runs/{id}/events            -> SSE hold-open
    GET  /runs/{id}/history?after=k   -> durable replay
    POST /runs/{id}/cancel            -> cancel a run
    POST /runs/{id}/tool_confirmation -> durable-HITL resume
    GET  /runs/{id}/file?path=<rel>   -> serve a fixture artifact (guarded)
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import Response, StreamingResponse

from warden.harness_api_mock.config import MockConfig
from warden.harness_api_mock.contract import (
    Event,
    RunAccepted,
    RunSpec,
    RunView,
    ToolConfirmation,
)
from warden.harness_api_mock.files import FileMissingError, PathGuardError
from warden.harness_api_mock.runner import MockRunner


def create_app(runner: MockRunner | None = None) -> FastAPI:
    """Build the mock Runs API around a (possibly injected) runner."""

    config = MockConfig()
    the_runner = runner or MockRunner(config)

    async def require_token(x_service_token: str | None = Header(default=None)) -> None:
        """Auth dependency on every route — a missing/wrong token is 401 (§4)."""
        if x_service_token != config.service_token:
            raise HTTPException(401, "invalid or missing service token")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await app.state.runner.init()
        yield
        await app.state.runner.aclose()

    app = FastAPI(title="Mock Harness Runs API", lifespan=lifespan)
    app.state.runner = the_runner
    app.state.config = config

    @app.get("/health")
    async def health() -> dict:
        """Unauthenticated liveness probe (for stack/compose wait_for)."""
        return {"status": "ok"}

    @app.post(
        "/runs", status_code=202, response_model=RunAccepted,
        dependencies=[Depends(require_token)],
    )
    async def start_run(spec: RunSpec) -> RunAccepted:
        if spec.sink.type == "webhook" and not spec.sink.url:
            raise HTTPException(422, "webhook sink requires a url")
        run_id = app.state.runner.submit(spec)
        return RunAccepted(run_id=run_id)

    @app.get(
        "/runs/{run_id}", response_model=RunView,
        dependencies=[Depends(require_token)],
    )
    async def get_run(run_id: str) -> RunView:
        view = app.state.runner.get(run_id)
        if view is None:
            raise HTTPException(404, f"unknown run {run_id}")
        return view

    @app.get("/runs/{run_id}/events", dependencies=[Depends(require_token)])
    async def stream_events(run_id: str) -> StreamingResponse:
        sse = app.state.runner.sse_for(run_id)
        if sse is None:
            raise HTTPException(404, f"no sse stream for run {run_id}")

        async def gen() -> AsyncIterator[str]:
            async for event in sse.stream():
                yield f"data: {json.dumps(event.model_dump())}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get(
        "/runs/{run_id}/history", response_model=list[Event],
        dependencies=[Depends(require_token)],
    )
    async def run_history(run_id: str, after: int = 0) -> list[Event]:
        return await app.state.runner.replay(run_id, after)

    @app.post("/runs/{run_id}/cancel", dependencies=[Depends(require_token)])
    async def cancel_run(run_id: str) -> dict:
        ok = await app.state.runner.cancel(run_id)
        if not ok:
            raise HTTPException(404, f"run {run_id} not cancellable")
        return {"run_id": run_id, "status": "cancelling"}

    @app.post(
        "/runs/{run_id}/tool_confirmation", dependencies=[Depends(require_token)],
    )
    async def confirm_tool(run_id: str, body: ToolConfirmation) -> dict:
        result = await app.state.runner.confirm(
            run_id, body.tool_use_id,
            allow=body.decision == "allow", reason=body.reason or "",
        )
        if result is None:
            raise HTTPException(404, f"unknown run {run_id}")
        return result

    @app.get("/runs/{run_id}/file", dependencies=[Depends(require_token)])
    async def read_file(run_id: str, path: str = Query(...)) -> Response:
        """Serve a run's fixture artifact bytes; guarded against traversal (§8)."""
        try:
            data = app.state.runner.read_file(run_id, path)
        except KeyError:
            raise HTTPException(404, f"unknown run {run_id}")
        except PathGuardError as exc:
            raise HTTPException(400, str(exc))
        except FileMissingError as exc:
            raise HTTPException(404, str(exc))
        return Response(content=data, media_type="application/octet-stream")

    return app


app = create_app()
