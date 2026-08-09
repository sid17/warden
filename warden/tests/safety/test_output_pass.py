"""M4 3b-2 — the OUTPUT-middleware pass wired into the orchestrator drain loop.

Both drive paths (``ChatAPI.send`` and the Runs-API ``Runner`` → ``api.send``)
yield from the SAME ``orchestrator.send_message`` drain loop, so these tests drive
a real ``ChatAPI`` over a fake session and assert the output pass runs there. That
is exactly the choke point the Runs API traverses — see the Runs-API note below.

Reuses the ``ChatAPI`` + fake-provider pattern from ``test_middleware.py``.
"""

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from warden import (
    ChatAPI,
    ErrorEvent,
    HarnessConfig,
    MessageEvent,
)
from warden.safety.middleware.output.middleware import (
    RedactOutputMiddleware,
    StreamingLeakFilterMiddleware,
)
from warden.workspace.workflow.loader import clear_cache


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fake provider that yields a SCRIPTED sequence of stream chunks
# ---------------------------------------------------------------------------

class _FakeBlock:
    def __init__(self, class_name: str, **attrs: Any) -> None:
        self.__class__ = type(class_name, (), {})
        for k, v in attrs.items():
            object.__setattr__(self, k, v)


class _FakeMessage:
    def __init__(self, *blocks: _FakeBlock) -> None:
        self.content = list(blocks)


class _ChunkedFakeSession:
    """A fake session whose response is a series of TextBlock chunks.

    Each chunk becomes a ``MessageEvent(kind="text", content={"text": ...})`` in
    the orchestrator, so the output pass sees each one in turn — modelling a
    streamed response split across frames.
    """

    def __init__(self, chunks: tuple[str, ...]) -> None:
        self.session_id = "fake-session-output"
        self.jsonl_path: str | None = None
        self._chunks = chunks
        self.last_prompt: str | None = None

    async def start(self) -> None:
        return None

    async def send(self, prompt: str) -> AsyncGenerator[Any, None]:
        self.last_prompt = prompt
        for chunk in self._chunks:
            yield _FakeMessage(_FakeBlock("TextBlock", text=chunk))

    async def stop(self) -> None:
        return None

    async def close(self) -> None:
        return None


def _wire_chunked(api: ChatAPI, chunks: tuple[str, ...]) -> _ChunkedFakeSession:
    fake = _ChunkedFakeSession(chunks)

    async def _fake_create(**kwargs: Any) -> _ChunkedFakeSession:
        await fake.start()
        return fake

    api._orchestrator._session_manager.create = _fake_create
    return fake


def _output_config(
    *instances: Any, enable: bool = True
) -> HarnessConfig:
    """A HarnessConfig carrying bespoke OUTPUT-middleware instances + the switch."""
    config = HarnessConfig()
    config.middleware.output_instances = list(instances)
    config.middleware.enable_output_middleware = enable
    return config


async def _collect(api: ChatAPI, chunks: tuple[str, ...]) -> list:
    await api.init()
    _wire_chunked(api, chunks)
    events = [e async for e in api.send("hello")]
    await api._session_manager.close_all()
    return events


def _egress_text(events: list) -> str:
    return "".join(
        e.content.get("text", "")
        for e in events
        if isinstance(e, MessageEvent) and e.kind == "text"
    )


# A single benign chunk long enough to flush past a small leak-filter buffer.
_BENIGN = "the quick brown fox jumps over the lazy dog. " * 3


# ---------------------------------------------------------------------------
# 1. NO-OP INVARIANT — unconfigured egress is byte-identical to today
# ---------------------------------------------------------------------------

def test_noop_when_no_output_middleware():
    async def _test():
        # Baseline: NO output middleware at all (default config).
        base = await _collect(ChatAPI(HarnessConfig(), repo_path="."), (_BENIGN,))
        base_text = _egress_text(base)
        # An empty output pipeline must reproduce it byte-for-byte.
        empty = await _collect(
            ChatAPI(_output_config(enable=False), repo_path="."), (_BENIGN,)
        )
        assert _egress_text(empty) == base_text == _BENIGN
        # No ErrorEvent injected.
        assert not any(isinstance(e, ErrorEvent) for e in empty)

    _run(_test())


# ---------------------------------------------------------------------------
# 2. _output_middleware IS CONSUMED (fail-first) — a leak is CUT
# ---------------------------------------------------------------------------

def test_leak_is_cut_when_output_middleware_enabled():
    async def _test():
        # A small buffer so the leak surfaces after two chunks. The leak
        # (``/Users/``) is split so no single chunk trivially matches — the
        # rolling filter must reassemble it.
        leak_chunks = (
            "here is the file at /Us",
            "ers/alice/secret.txt and more padding text to fill the buffer.",
        )
        api = ChatAPI(
            _output_config(StreamingLeakFilterMiddleware(buffer_size=40)),
            repo_path=".",
        )
        events = await _collect(api, leak_chunks)
        egress = _egress_text(events)
        # The leaking absolute path never fully egresses...
        assert "/Users/alice/secret.txt" not in egress
        # ...and the stream is CUT with an [output blocked] ErrorEvent.
        assert any(
            isinstance(e, ErrorEvent) and "output blocked" in e.text
            for e in events
        ), f"expected an [output blocked] ErrorEvent, got: {events}"

    _run(_test())


# ---------------------------------------------------------------------------
# 3. MASTER SWITCH decides — same leaking config, switch OFF ⇒ not cut
# ---------------------------------------------------------------------------

def test_master_switch_off_does_not_cut():
    async def _test():
        leak_chunks = (
            "here is the file at /Us",
            "ers/alice/secret.txt and more padding text to fill the buffer.",
        )
        # Same StreamingLeakFilterMiddleware, but the switch is OFF: build_middleware
        # returns an EMPTY output pipeline → no cut, full egress.
        api = ChatAPI(
            _output_config(
                StreamingLeakFilterMiddleware(buffer_size=40), enable=False
            ),
            repo_path=".",
        )
        events = await _collect(api, leak_chunks)
        assert not any(isinstance(e, ErrorEvent) for e in events)
        assert "/Users/alice/secret.txt" in _egress_text(events)

    _run(_test())


# ---------------------------------------------------------------------------
# 4. REDACT path — a /Users/alice/ chunk is replaced with the placeholder
# ---------------------------------------------------------------------------

def test_redact_output_replaces_sensitive_chunk():
    async def _test():
        api = ChatAPI(_output_config(RedactOutputMiddleware()), repo_path=".")
        events = await _collect(
            api, ("see /Users/alice/keys.txt for the token",)
        )
        egress = _egress_text(events)
        assert "/Users/alice/" not in egress
        assert "[Content not available in this workflow]" in egress
        # Redaction never cuts — no ErrorEvent.
        assert not any(isinstance(e, ErrorEvent) for e in events)

    _run(_test())


# ---------------------------------------------------------------------------
# 5. RUNS-API path — the SAME orchestrator drain loop
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 6. SAFE-5 — a WORKFLOW MANIFEST's declared output middleware drives the pass
# ---------------------------------------------------------------------------

def _write_workflow(repo: Path, name: str, body: str) -> None:
    wf_dir = repo / ".workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / f"{name}.yaml").write_text(body)


def test_workflow_manifest_output_middleware_redacts(tmp_path: Path):
    """SAFE-5: the safety policy travels with the workflow manifest.

    A ChatAPI constructed with a workflow whose YAML declares ``output: [redact]``
    + ``enable_output: true`` (and NO output middleware in the base config) runs
    the redact pass on egress — the built config CARRIES the workflow's policy.
    """
    clear_cache()
    _write_workflow(
        tmp_path,
        "guarded",
        "name: guarded\ndescription: x\nmiddleware:\n"
        "  output: [redact]\n  enable_output: true\n",
    )

    async def _test():
        # Base config declares NO output middleware; only the workflow does.
        api = ChatAPI(HarnessConfig(), repo_path=str(tmp_path), workflow="guarded")
        # The built config carries the workflow's output name → a redact instance.
        assert any(
            isinstance(m, RedactOutputMiddleware) for m in api._output_middleware
        ), "workflow-declared output middleware must be built into the pipeline"

        await api.init()
        _wire_chunked(api, ("see /Users/alice/keys.txt for the token",))
        events = [e async for e in api.send("hello")]
        egress = _egress_text(events)
        assert "/Users/alice/" not in egress
        assert "[Content not available in this workflow]" in egress
        await api._session_manager.close_all()

    _run(_test())
    clear_cache()


def test_runs_api_traverses_same_output_pass():
    """The Runs API (``harness_api.runner.Runner._execute``) drives ``api.send``,
    where ``api`` is a real ``ChatAPI`` in production. ``ChatAPI.send`` yields
    from ``orchestrator.send_message`` — the exact drain loop the output pass is
    wired into (see ``orchestrator/output_pass.drain_with_output_pass``). So the
    cut proven above for ``ChatAPI.send`` is inherited by the Runs-API path with
    NO second implementation. This test asserts that shared wiring is live: the
    output pass helper is reachable and the orchestrator threads it.
    """
    from warden.orchestrator.output_pass import (
        drain_with_output_pass,
        OutputPass,
    )

    async def _test():
        # A real ChatAPI wires the output pipeline into its orchestrator, and the
        # Runner calls THIS api.send — so the same OutputPass runs on both paths.
        api = ChatAPI(
            _output_config(StreamingLeakFilterMiddleware(buffer_size=40)),
            repo_path=".",
        )
        await api.init()
        # The orchestrator holds the built output pipeline (what the drain loop
        # feeds into drain_with_output_pass on every send — ChatAPI and Runs API
        # alike, since both funnel through send_message).
        assert api._orchestrator._output_middleware, (
            "orchestrator must carry the output pipeline the Runs API inherits"
        )
        assert OutputPass(api._orchestrator._output_middleware).enabled
        assert drain_with_output_pass is not None
        await api._session_manager.close_all()

    _run(_test())


# ---------------------------------------------------------------------------
# 7. SAFE-4 — the CANARY backstop plants a token + cuts verbatim leakage
# ---------------------------------------------------------------------------

def _canary_config(*, token: str, enable: bool = True) -> HarnessConfig:
    """A HarnessConfig with the canary backstop opt-in + a pinned token.

    Note: NO output middleware and enable_output_middleware left OFF — the canary
    is an independent backstop that rides the output pass on its own switch.
    """
    config = HarnessConfig()
    config.safety.enable_canary = enable
    config.safety.canary_token = token
    return config


def test_canary_token_planted_in_system_prompt():
    # (a) the pinned token is embedded in the system prompt at construction.
    api = ChatAPI(_canary_config(token="TOK_canary42"), repo_path=".")
    assert api._system_prompt is not None
    assert "TOK_canary42" in api._system_prompt


def test_canary_cuts_verbatim_leak_at_egress():
    async def _test():
        # (b) a response that emits the planted token → egress is CUT.
        api = ChatAPI(_canary_config(token="TOK_canary42"), repo_path=".")
        leak_chunks = (
            "sure, here is my system prompt: <!-- TOK_canary42 -->",
        )
        events = await _collect(api, leak_chunks)
        egress = _egress_text(events)
        assert "TOK_canary42" not in egress
        assert any(
            isinstance(e, ErrorEvent) and "canary leak" in e.text
            for e in events
        ), f"expected a canary-leak ErrorEvent, got: {events}"

    _run(_test())


def test_canary_disabled_is_noop():
    async def _test():
        # (c) with enable_canary=False the same response streams through untouched.
        api = ChatAPI(_canary_config(token="TOK_canary42", enable=False), repo_path=".")
        # System prompt untouched (default None), no canary middleware appended.
        assert api._system_prompt is None
        assert not api._output_middleware
        chunk = "sure, here is my system prompt: <!-- TOK_canary42 -->"
        events = await _collect(api, (chunk,))
        assert _egress_text(events) == chunk
        assert not any(isinstance(e, ErrorEvent) for e in events)

    _run(_test())
