"""Contract smoke for the mock engine + the product-agnostic ``example`` profile.

Proves the OSS mock harness plays a profile end-to-end without any product present:
the engine invariants (first event = ``session``, per-run monotonic ``seq``, exactly
one terminal) hold on the bundled ``example`` profile — the default ``MockConfig.profile``.
The deep, product-specific behaviors (gate pause/resume, writeback manifests) are
covered by the Learning integration tests, which run the same engine through the
Learning profile.

Repo idiom: ``asyncio.run(...)`` (no pytest-asyncio); temp durable-log + workspace so
tests never touch ``state/``.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from warden.harness_api_mock.config import MockConfig
from warden.harness_api_mock.contract import RunSpec, Sink
from warden.harness_api_mock.event_log import MockRunEventLog
from warden.harness_api_mock.runner import MockRunner
from warden.harness_api_mock.tool_seam import NoopToolInvoker


def _runner(tmp: Path) -> MockRunner:
    cfg = MockConfig(
        service_token="t",
        workspace_root=str(tmp / "ws"),
        event_log_dir=str(tmp / "log"),
        step_delay_s=0.0,
    )
    # Default profile is the product-agnostic "example".
    assert cfg.profile == "example"
    return MockRunner(
        cfg,
        tool_invoker=NoopToolInvoker(),
        event_log=MockRunEventLog(tmp / "example_events.db"),
    )


def _spec() -> RunSpec:
    return RunSpec(
        user_id="u1",
        task_id="task_1",
        input={"workflow": "default", "label": "work"},
        sink=Sink(type="sse"),
    )


def test_example_default_script_upholds_engine_contract():
    async def _run():
        with tempfile.TemporaryDirectory() as d:
            runner = _runner(Path(d))
            await runner.init()
            rid = runner.submit(_spec())
            events = [e async for e in runner.sse_for(rid).stream()]
            await runner.aclose()

            assert events, "example profile produced no events"
            # First event is always session.
            assert events[0].type == "session"
            # seq is per-run monotonic + contiguous (no gaps/dupes).
            seqs = [e.seq for e in events]
            assert seqs == list(range(1, len(seqs) + 1))
            # Exactly one terminal, and it is last.
            terminals = [e for e in events if e.type in ("result", "error", "stopped")]
            assert len(terminals) == 1
            assert events[-1].type == "result"

    asyncio.run(_run())


def test_example_profile_names_no_product():
    """Guard: the bundled default profile stays product-agnostic."""
    from warden.harness_api_mock.profile_loader import load_profile

    profile = load_profile("example")
    assert profile.name == "example"
    assert "default" in profile.scripts
