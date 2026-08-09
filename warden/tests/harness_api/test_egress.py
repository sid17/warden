"""Egress retry/failure-count + run-cancellation tests.

The webhook *happy path* (ordered delivery) lives in ``test_runs_api.py``. This
file covers the two branches that file does not exercise:

- ``WebhookEgress.emit`` **retry / failure-count** branch (egress.py L66): a POST
  that keeps returning 5xx / raising is retried up to ``max_attempts``, then
  **logged + counted** in ``delivery_failures`` — never re-raised (the documented
  LAW-4 escape hatch, egress.py L44: the run's work is done, the product owns
  durable redelivery).
- Run **cancellation** (app.py ``cancel_run`` L63 -> ``Runner.cancel`` ->
  the ``asyncio.CancelledError`` branch in ``Runner._run``): a cancelled run
  sets status ``cancelled`` and emits a terminal event carrying
  ``{"reason": "cancelled"}``.

Hermetic: the HTTP client is a fake (no real network), ``asyncio.sleep`` is
patched so backoff adds no wall-clock delay, and cancellation is driven through
the mock skill's ``gate`` (reused from ``test_runs_api.py``) — no LLM, no
subprocess. Follows the repo's ``asyncio.run(_run())`` style (no pytest-asyncio).
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from warden.harness_api.egress import WebhookEgress
from warden.harness_api.schemas import Event, Sink
from warden.tests.harness_api.mock_skill import Tracker, build_factory
from warden.tests.harness_api.test_runs_api import (
    _KEYS,
    _spec,
    _tmp_event_log,
    _ungoverned_config,
)
from warden.harness_api.runner import Runner


# --- fakes / helpers ------------------------------------------------------


def _event(seq: int = 1) -> Event:
    """A minimal typed event to push through an egress adapter."""
    return Event(run_id="run_test", seq=seq, type="token", data={"text": "hi"}, at="t")


class _FakeResponse:
    """Stands in for ``httpx.Response`` — only what ``emit`` touches."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        # ``emit`` builds an HTTPStatusError from ``resp.request``/``resp`` on 4xx+.
        self.request = httpx.Request("POST", "http://rx/hook")


class _FakeClient:
    """Async HTTP client double for ``WebhookEgress``.

    ``behavior`` is one of: an int status code returned for every POST, or an
    ``Exception`` instance raised on every POST. Records each POST call so tests
    can assert the retry count.
    """

    def __init__(self, behavior) -> None:
        self._behavior = behavior
        self.calls: list[dict] = []

    async def post(self, url, *, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if isinstance(self._behavior, Exception):
            raise self._behavior
        return _FakeResponse(self._behavior)


class _CountingClient(_FakeClient):
    """Fails the first ``fail_first`` POSTs (returns 500), then succeeds (200)."""

    def __init__(self, fail_first: int) -> None:
        super().__init__(200)
        self._fail_first = fail_first

    async def post(self, url, *, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if len(self.calls) <= self._fail_first:
            return _FakeResponse(500)
        return _FakeResponse(200)


def _no_sleep_patch():
    """Patch ``asyncio.sleep`` inside egress so backoff costs no wall time.

    Returns a restore callable. We swap the name the module actually calls
    (``egress.asyncio.sleep``) rather than the global, keeping it surgical.
    """
    import warden.harness_api.egress as egress_mod

    original = egress_mod.asyncio.sleep
    slept: list[float] = []

    async def _fake_sleep(delay):
        slept.append(delay)

    egress_mod.asyncio.sleep = _fake_sleep

    def restore():
        egress_mod.asyncio.sleep = original

    return restore, slept


# --- WebhookEgress.emit: success (no failure increment) -------------------


def test_emit_success_first_try_no_failure_increment():
    async def _run():
        client = _FakeClient(200)
        egress = WebhookEgress("http://rx/hook", client=client)
        await egress.emit(_event())
        # One POST, no retry, nothing counted as a failure.
        assert len(client.calls) == 1
        assert egress.delivery_failures == 0
        assert client.calls[0]["url"] == "http://rx/hook"

    asyncio.run(_run())


def test_emit_forwards_headers_and_payload():
    async def _run():
        client = _FakeClient(200)
        egress = WebhookEgress(
            "http://rx/hook", headers={"X-Auth": "tok"}, client=client
        )
        ev = _event(seq=7)
        await egress.emit(ev)
        call = client.calls[0]
        assert call["headers"] == {"X-Auth": "tok"}
        assert call["json"] == ev.model_dump()  # exact wire payload

    asyncio.run(_run())


# --- WebhookEgress.emit: retry on 5xx, then count + log (no raise) ---------


def test_emit_500_retries_to_limit_counts_and_logs(caplog=None):
    async def _run():
        restore, slept = _no_sleep_patch()
        try:
            client = _FakeClient(500)  # every attempt returns a server error
            egress = WebhookEgress("http://rx/hook", client=client, max_attempts=3)
            with _capture_error_logs() as records:
                # Must NOT raise — accounts, does not crash the run.
                await egress.emit(_event())
            # Exhausted all attempts.
            assert len(client.calls) == 3
            # Backoff slept between attempts only (2 sleeps for 3 attempts).
            assert slept == [0.2 * 1, 0.2 * 2]
            # Counted exactly one delivery failure.
            assert egress.delivery_failures == 1
            # Surfaced, not swallowed (LAW 4): one error log line.
            assert len(records) == 1
            assert "webhook delivery failed" in records[0].getMessage()
        finally:
            restore()

    asyncio.run(_run())


def test_emit_raise_retries_to_limit_counts(caplog=None):
    async def _run():
        restore, slept = _no_sleep_patch()
        try:
            boom = httpx.ConnectError("connection refused")
            client = _FakeClient(boom)  # every attempt raises
            egress = WebhookEgress("http://rx/hook", client=client, max_attempts=3)
            with _capture_error_logs() as records:
                await egress.emit(_event())  # does not propagate the ConnectError
            assert len(client.calls) == 3
            assert egress.delivery_failures == 1
            assert len(records) == 1
            # The final exception is included in the log for triage.
            assert "connection refused" in records[0].getMessage()
        finally:
            restore()

    asyncio.run(_run())


def test_emit_recovers_before_limit_no_failure():
    async def _run():
        restore, slept = _no_sleep_patch()
        try:
            # Fail twice (500), succeed on the 3rd attempt.
            client = _CountingClient(fail_first=2)
            egress = WebhookEgress("http://rx/hook", client=client, max_attempts=3)
            await egress.emit(_event())
            assert len(client.calls) == 3  # two failures + one success
            assert egress.delivery_failures == 0  # recovered -> not counted
            assert slept == [0.2 * 1, 0.2 * 2]  # slept after the two failures
        finally:
            restore()

    asyncio.run(_run())


def test_emit_failures_accumulate_across_calls():
    async def _run():
        restore, _ = _no_sleep_patch()
        try:
            client = _FakeClient(503)
            egress = WebhookEgress("http://rx/hook", client=client, max_attempts=2)
            with _capture_error_logs():
                await egress.emit(_event(seq=1))
                await egress.emit(_event(seq=2))
            # Each failing emit increments the running counter independently.
            assert egress.delivery_failures == 2
            assert len(client.calls) == 4  # 2 emits * 2 attempts
        finally:
            restore()

    asyncio.run(_run())


def test_emit_single_attempt_no_backoff_sleep():
    async def _run():
        restore, slept = _no_sleep_patch()
        try:
            client = _FakeClient(500)
            egress = WebhookEgress("http://rx/hook", client=client, max_attempts=1)
            with _capture_error_logs():
                await egress.emit(_event())
            assert len(client.calls) == 1
            assert slept == []  # no attempt remains, so no backoff sleep
            assert egress.delivery_failures == 1
        finally:
            restore()

    asyncio.run(_run())


# --- log-capture helper ---------------------------------------------------


class _capture_error_logs:
    """Context manager capturing ERROR records from the egress logger."""

    def __init__(self) -> None:
        self._handler = _ListHandler()
        self._logger = logging.getLogger("warden.harness_api.egress")

    def __enter__(self):
        self._prev_level = self._logger.level
        self._logger.setLevel(logging.ERROR)
        self._logger.addHandler(self._handler)
        return self._handler.records

    def __exit__(self, *exc):
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._prev_level)
        return False


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.ERROR:
            self.records.append(record)


# --- run cancellation: terminal cancelled status + event ------------------


def _make_runner(tracker, **mock_kwargs):
    return Runner(
        _ungoverned_config(),
        keys=_KEYS,
        chat_api_factory=build_factory(tracker, **mock_kwargs),
        event_log=_tmp_event_log(),
    )


def test_cancel_running_run_emits_cancelled_terminal():
    async def _run():
        tracker = Tracker()
        tracker.started = asyncio.Event()
        gate = asyncio.Event()  # holds the mock skill mid-stream (never released)
        runner = _make_runner(tracker, gate=gate)

        run_id = runner.submit(_spec("u1", "course_42", sink=Sink(type="sse")))
        # Wait until the run is actually executing (gated inside the skill).
        await asyncio.sleep(0)  # let the task start
        # Poll for the run to reach "running" before cancelling.
        for _ in range(200):
            if runner.get(run_id).status == "running":
                break
            await asyncio.sleep(0)

        ok = await runner.cancel(run_id)
        assert ok is True

        # Let the CancelledError propagate through _run's handler.
        task = runner.task_for(run_id)
        await task

        # Status flipped to cancelled; the run recorded the reason.
        view = runner.get(run_id)
        assert view.status == "cancelled"
        assert view.error == "cancelled"

        # The SSE stream carries a terminal event with reason "cancelled".
        # (Its wire type is "error" — the cancel is surfaced via data.reason.)
        events = [e async for e in runner.sse_for(run_id).stream()]
        assert events[-1].type == "error"
        assert events[-1].data == {"reason": "cancelled"}

    asyncio.run(_run())


def test_cancel_unknown_or_finished_run_returns_false():
    async def _run():
        tracker = Tracker()
        runner = _make_runner(tracker)

        # Unknown run id -> not cancellable.
        assert await runner.cancel("run_nope") is False

        # A finished run -> its task is done -> not cancellable.
        run_id = runner.submit(_spec("u1", "course_done", sink=Sink(type="sse")))
        await runner.task_for(run_id)
        assert runner.get(run_id).status == "succeeded"
        assert await runner.cancel(run_id) is False

    asyncio.run(_run())


def test_cancel_run_via_app_route():
    async def _run():
        from warden.harness_api.app import create_app

        tracker = Tracker()
        tracker.started = asyncio.Event()
        gate = asyncio.Event()
        runner = _make_runner(tracker, gate=gate)
        app = create_app(runner)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.post(
                "/runs",
                json={
                    "user_id": "u1",
                    "task_id": "course_42",
                    "input": {"prompt": "hi"},
                    "sink": {"type": "sse"},
                },
            )
            run_id = resp.json()["run_id"]
            for _ in range(200):
                if runner.get(run_id).status == "running":
                    break
                await asyncio.sleep(0)

            cancel = await client.post(f"/runs/{run_id}/cancel")
            assert cancel.status_code == 200
            assert cancel.json() == {"run_id": run_id, "status": "cancelling"}

            await runner.task_for(run_id)
            assert (await client.get(f"/runs/{run_id}")).json()["status"] == "cancelled"

            # Unknown run -> 404 (cancel_run's not-cancellable branch).
            missing = await client.post("/runs/run_missing/cancel")
            assert missing.status_code == 404

    asyncio.run(_run())
