"""E2E tests for CLI providers — real CLI subprocess through orchestrator.

These tests spawn a real `codex` CLI process. They are skipped if the CLI is
not installed on the machine.

The `claude -p` CLI provider was retired (decision D7) in favour of the Claude
SDK provider (`provider='claude'`); its former e2e/parity tests were removed
when the path was deleted. SDK-provider behaviour is covered by the provider
unit/integration suites.

Run: uv run --no-sync python -m pytest warden/tests/e2e/test_cli_providers_e2e.py -v
"""

import asyncio
import shutil
from pathlib import Path

import pytest

from warden.orchestrator.orchestrator import Orchestrator
from warden.orchestrator.session.manager import SessionManager
from warden.schemas.events import (
    CompletionEvent,
    MessageEvent,
)

CODEX_AVAILABLE = shutil.which("codex") is not None


# --- Helpers ---

async def _make_orch():
    """Create an Orchestrator with initialized SessionManager."""
    sm = SessionManager()
    await sm.init()
    return Orchestrator(session_manager=sm, repo_path=Path("."))


async def _collect(orch, prompt, provider, session_id=None):
    """Send a prompt and collect all events."""
    events = []
    async for event in orch.send_message(
        prompt, provider=provider, session_id=session_id,
    ):
        events.append(event)
    return events


def _msg_events(events):
    return [e for e in events if isinstance(e, MessageEvent)]


def _completion(events):
    return [e for e in events if isinstance(e, CompletionEvent)]


# =========================================================================
# Codex E2E
# =========================================================================

@pytest.mark.skipif(not CODEX_AVAILABLE, reason="codex CLI not installed")
class TestCodexE2E:

    def test_codex_simple_prompt(self):
        """Codex provider streams events for a simple prompt."""
        async def _run():
            orch = await _make_orch()
            events = await _collect(orch, "respond with exactly: CODEX_OK", "codex")

            assert len(_msg_events(events)) > 0, "No MessageEvents from Codex"
            assert len(_completion(events)) == 1, "Should get CompletionEvent"
            await orch.close()

        asyncio.run(_run())
