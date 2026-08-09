"""Regression: the Claude session raises the SDK's 1 MB control-message ceiling.

A single large tool payload (a full chapter ``write``, or a research sub-agent
returning big context) can exceed the SDK's default 1 MB stdin buffer. When it
does, the SDK's decoder raises "JSON message exceeded maximum buffer size",
closes the input stream, and rejects every pending permission request → the run
dies mid-turn with the "Error in hook callback / Stream closed" cascade. The
session must pass a generous ``max_buffer_size`` so realistic agent payloads fit.

This is DISTINCT from the durable-defer hook-timeout crash (bf1e4527) — same
symptom, different cause. Live-surfaced by the Task 3b UI E2E (a create whose LLM
emitted a >1 MB tool payload), which the 5 prior gated creates had dodged by luck.
"""

from __future__ import annotations

import pytest

from warden.providers.claude import session as claude_session
from warden.providers.claude.session import (
    _MAX_CONTROL_MESSAGE_BYTES,
    ClaudeSession,
)


class _FakeClient:
    """Captures the ClaudeAgentOptions handed to the SDK; no real subprocess."""

    captured_options = None

    def __init__(self, options):
        _FakeClient.captured_options = options

    async def connect(self):  # the SDK client's async connect — no-op here
        return None


@pytest.mark.asyncio
async def test_start_raises_control_message_buffer_ceiling(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_session, "ClaudeSDKClient", _FakeClient)

    sess = ClaudeSession(repo_path=tmp_path)
    await sess.start()

    opts = _FakeClient.captured_options
    assert opts is not None, "start() never constructed the SDK client"
    # The ceiling is set and well above the SDK's 1 MB default (which would crash
    # on a large tool payload).
    assert opts.max_buffer_size == _MAX_CONTROL_MESSAGE_BYTES
    assert opts.max_buffer_size > 1 * 1024 * 1024


def test_ceiling_is_generous_headroom_over_the_sdk_default():
    # Guard the constant itself so a future edit can't silently drop it back near
    # the 1 MB default that caused the crash.
    assert _MAX_CONTROL_MESSAGE_BYTES >= 16 * 1024 * 1024
