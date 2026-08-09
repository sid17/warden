"""Hermetic unit tests for the Claude Langfuse tracer.

No real Langfuse / OTel / network. The `trace` / `lf_client` params are
duck-typed Langfuse objects, faked with MagicMocks that record
`.span()` / `.generation()` / `.end()` / `.update()`; we assert on those.
`langfuse` is not installed here; `_create_generation` does a lazy
`from langfuse.model import ModelUsage`, so a fixture injects a fake module.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from warden.observability.telemetry.claude_langfuse_tracer import (
    ClaudeLangfuseTracer,
    _get_field,
)

MODULE = "warden.observability.telemetry.claude_langfuse_tracer"


class _Bag:
    """Attribute bag — every kwarg becomes an attribute."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _named(name):
    """Return a _Bag subclass whose __name__ is exactly ``name`` (SDK dispatch)."""
    return type(name, (_Bag,), {})


UserMessage = _named("UserMessage")
AssistantMessage = _named("AssistantMessage")
TaskStartedMessage = _named("TaskStartedMessage")
TaskProgressMessage = _named("TaskProgressMessage")
TaskNotificationMessage = _named("TaskNotificationMessage")
ResultMessage = _named("ResultMessage")

TextBlock = _named("TextBlock")
ThinkingBlock = _named("ThinkingBlock")
ToolUseBlock = _named("ToolUseBlock")
ServerToolUseBlock = _named("ServerToolUseBlock")
ServerToolResultBlock = _named("ServerToolResultBlock")
ToolResultBlock = _named("ToolResultBlock")


@pytest.fixture(autouse=True)
def _fake_langfuse_model(monkeypatch):
    """Inject a fake ``langfuse.model.ModelUsage`` so lazy import succeeds."""
    fake_pkg = types.ModuleType("langfuse")
    fake_model = types.ModuleType("langfuse.model")

    class ModelUsage:  # noqa: N801 — mirror real name
        def __init__(self, **kw):
            self.kw = kw

    fake_model.ModelUsage = ModelUsage
    fake_pkg.model = fake_model
    monkeypatch.setitem(sys.modules, "langfuse", fake_pkg)
    monkeypatch.setitem(sys.modules, "langfuse.model", fake_model)
    return ModelUsage


@pytest.fixture
def trace():
    return MagicMock(name="trace")


@pytest.fixture
def lf_client():
    return MagicMock(name="lf_client")


@pytest.fixture
def tracer(trace, lf_client):
    return ClaudeLangfuseTracer(trace, lf_client)


def _new_span(mock_parent):
    """Make ``mock_parent.span()`` return a fresh recording mock each call."""
    spans = []

    def factory(*a, **k):
        s = MagicMock(name=f"span{len(spans)}")
        spans.append(s)
        return s

    mock_parent.span.side_effect = factory
    return spans


class TestGetField:
    def test_prefers_attr(self):
        msg = _Bag(foo="attr")
        assert _get_field(msg, "foo", {"foo": "data"}) == "attr"

    def test_falls_back_to_data(self):
        msg = _Bag()
        assert _get_field(msg, "foo", {"foo": "data"}) == "data"

    def test_none_when_missing_everywhere(self):
        assert _get_field(_Bag(), "foo", None) is None
        assert _get_field(_Bag(), "foo", {}) is None

    def test_attr_none_falls_through_to_data(self):
        msg = _Bag(foo=None)
        assert _get_field(msg, "foo", {"foo": "data"}) == "data"


class TestExtractResultText:
    fn = staticmethod(ClaudeLangfuseTracer._extract_result_text)

    def test_str_passthrough(self):
        assert self.fn("hello") == "hello"

    def test_none_returns_empty(self):
        assert self.fn(None) == ""

    def test_list_of_dicts_prefers_text(self):
        out = self.fn([{"text": "one"}, {"content": "two"}])
        assert out == "one\ntwo"

    def test_list_of_objects_uses_content_attr(self):
        obj = _Bag(content="blockcontent")
        assert self.fn([obj]) == "blockcontent"

    def test_non_str_non_list_stringified(self):
        assert self.fn(42) == "42"


class TestCreate:
    def test_returns_none_when_langfuse_disabled(self, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.get_langfuse", lambda *a: None)
        assert ClaudeLangfuseTracer.create("sid", "prompt") is None

    def test_builds_trace_when_enabled(self, monkeypatch):
        lf = MagicMock()
        monkeypatch.setattr(f"{MODULE}.get_langfuse", lambda *a: lf)
        tr = ClaudeLangfuseTracer.create("sid-1", "the prompt")
        assert isinstance(tr, ClaudeLangfuseTracer)
        lf.trace.assert_called_once()
        kwargs = lf.trace.call_args.kwargs
        assert kwargs["session_id"] == "sid-1"
        assert kwargs["input"] == "the prompt"
        assert kwargs["metadata"] == {"provider": "claude"}
        assert tr._lf is lf

    def test_update_session_id(self, tracer, trace):
        tracer.update_session_id("new-sid")
        trace.update.assert_called_once_with(session_id="new-sid")


class TestDispatch:
    def test_unknown_type_is_noop(self, tracer, trace):
        tracer.handle_message(_named("MysteryMessage")())
        trace.generation.assert_not_called()
        trace.span.assert_not_called()
        trace.update.assert_not_called()

    def test_result_message_routes_to_handler(self, tracer, trace):
        tracer.handle_message(
            ResultMessage(result="final answer", total_cost_usd=0.42, num_turns=3)
        )
        assert tracer.output_text == "final answer"
        assert tracer.total_cost_usd == 0.42
        trace.update.assert_called_once()
        assert trace.update.call_args.kwargs["metadata"]["num_turns"] == 3

    def test_assistant_message_routes_and_makes_generation(self, tracer, trace):
        tracer.handle_message(
            AssistantMessage(
                model="claude-x",
                content=[TextBlock(text="hi")],
                usage={"input_tokens": 5, "output_tokens": 7},
            )
        )
        trace.generation.assert_called_once()


class TestGenerationAttribution:
    def test_tokens_model_and_output_accumulated(self, tracer, trace):
        msg = AssistantMessage(
            model="claude-opus",
            content=[TextBlock(text="hello world")],
            usage={
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read_input_tokens": 4,
            },
            stop_reason="end_turn",
            message_id="m1",
        )
        tracer.handle_message(msg)

        assert tracer.model_name == "claude-opus"
        assert tracer.total_input_tokens == 10
        assert tracer.total_output_tokens == 20
        assert tracer.cache_read_tokens == 4
        assert tracer.output_text == "hello world"
        assert tracer._llm_call_count == 1

        gen = trace.generation.call_args.kwargs
        assert gen["name"] == "llm_call_1"
        assert gen["model"] == "claude-opus"
        assert gen["output"] == "hello world"
        assert gen["metadata"]["stop_reason"] == "end_turn"
        assert gen["metadata"]["message_id"] == "m1"
        assert gen["metadata"]["content_blocks"] == ["TextBlock"]

    def test_two_calls_accumulate_and_increment_counter(self, tracer):
        for _ in range(2):
            tracer.handle_message(
                AssistantMessage(
                    model="m",
                    content=[TextBlock(text="x")],
                    usage={"input_tokens": 3, "output_tokens": 2},
                )
            )
        assert tracer.total_input_tokens == 6
        assert tracer.total_output_tokens == 4
        assert tracer._llm_call_count == 2

    def test_thinking_block_captured_in_output(self, tracer, trace):
        tracer.handle_message(
            AssistantMessage(
                model="m",
                content=[ThinkingBlock(thinking="deep thought")],
                usage={},
            )
        )
        out = trace.generation.call_args.kwargs["output"]
        assert "[thinking] deep thought" in out

    def test_falls_back_to_stored_model_name(self, tracer, trace):
        tracer.model_name = "prev-model"
        tracer.handle_message(
            AssistantMessage(model=None, content=[TextBlock(text="t")], usage={})
        )
        assert trace.generation.call_args.kwargs["model"] == "prev-model"


class TestToolSpans:
    def test_tool_use_block_opens_span_on_trace(self, tracer, trace):
        spans = _new_span(trace)
        tracer.handle_message(
            AssistantMessage(
                model="m",
                content=[ToolUseBlock(id="t1", name="Bash", input={"cmd": "ls"})],
                usage={},
            )
        )
        assert "t1" in tracer._open_tool_spans
        trace.span.assert_called_once()
        assert trace.span.call_args.kwargs["name"] == "tool: Bash"
        assert trace.span.call_args.kwargs["metadata"]["tool_call_id"] == "t1"
        assert spans  # a span object was produced

    def test_user_message_closes_open_tool_span_with_result(self, tracer, trace):
        _new_span(trace)
        tracer.handle_message(
            AssistantMessage(
                model="m",
                content=[ToolUseBlock(id="t1", name="Bash", input={})],
                usage={},
            )
        )
        span = tracer._open_tool_spans["t1"]
        # Result comes back as a UserMessage (no parent, no tool_use_result).
        tracer.handle_message(UserMessage(content="the result text"))
        span.end.assert_called_once()
        assert "t1" not in tracer._open_tool_spans
        assert span.end.call_args.kwargs["output"] == "the result text"

    def test_server_tool_result_closes_matching_span(self, tracer, trace):
        _new_span(trace)
        tracer.handle_message(
            AssistantMessage(
                model="m",
                content=[ServerToolUseBlock(id="s1", name="web", input={})],
                usage={},
            )
        )
        span = tracer._open_tool_spans["s1"]
        tracer.handle_message(
            AssistantMessage(
                model="m",
                content=[ServerToolResultBlock(tool_use_id="s1", content={"r": 1})],
                usage={},
            )
        )
        span.end.assert_called_once()
        assert "s1" not in tracer._open_tool_spans


class TestStaleSpanCleanup:
    def test_new_assistant_msg_closes_prior_main_tool_span(self, tracer, trace):
        _new_span(trace)
        tracer.handle_message(
            AssistantMessage(
                model="m",
                content=[ToolUseBlock(id="t1", name="Bash", input={})],
                usage={},
            )
        )
        stale = tracer._open_tool_spans["t1"]
        # A second assistant message (no result in between) should close it.
        tracer.handle_message(
            AssistantMessage(model="m", content=[TextBlock(text="ok")], usage={})
        )
        stale.end.assert_called_once()
        assert "t1" not in tracer._open_tool_spans


class TestSubAgentNesting:
    def _open_agent(self, tracer, trace):
        agent_span = MagicMock(name="agent_span")
        child_spans = []

        def parent_factory(*a, **k):
            # First span() is the Agent span; child spans come off it.
            if k.get("name") == "tool: Agent":
                agent_span.span.side_effect = lambda *aa, **kk: _record(child_spans)
                return agent_span
            return _record(child_spans)

        def _record(store):
            s = MagicMock(name=f"child{len(store)}")
            store.append(s)
            return s

        trace.span.side_effect = parent_factory
        tracer.handle_message(
            AssistantMessage(
                model="m",
                content=[
                    ToolUseBlock(
                        id="agent1",
                        name="Agent",
                        input={"description": "d", "subagent_type": "researcher"},
                    )
                ],
                usage={"input_tokens": 1, "output_tokens": 1},
            )
        )
        return agent_span, child_spans

    def test_agent_block_creates_agent_span(self, tracer, trace):
        agent_span, _ = self._open_agent(tracer, trace)
        assert tracer._agent_spans["agent1"] is agent_span
        # named "tool: Agent" with subagent metadata
        agent_call = [
            c for c in trace.span.call_args_list if c.kwargs.get("name") == "tool: Agent"
        ]
        assert agent_call
        assert agent_call[0].kwargs["metadata"]["subagent_type"] == "researcher"

    def test_child_message_nests_under_agent_span(self, tracer, trace):
        agent_span, child_spans = self._open_agent(tracer, trace)
        # A sub-agent assistant message with parent_id -> obs_parent is agent_span.
        tracer.handle_message(
            AssistantMessage(
                model="m",
                content=[ToolUseBlock(id="c1", name="Read", input={})],
                usage={"input_tokens": 100, "output_tokens": 100},
                parent_tool_use_id="agent1",
            )
        )
        # Generation created on the agent span, not the trace.
        agent_span.generation.assert_called_once()
        # Child tool span keyed by "parent:tool_id".
        assert "agent1:c1" in tracer._open_tool_spans
        # Sub-agent tokens must NOT leak into the top-level totals.
        assert tracer.total_input_tokens == 1  # only the Agent open message counted
        assert tracer.total_output_tokens == 1

    def test_child_tool_result_closes_prefixed_span(self, tracer, trace):
        agent_span, _ = self._open_agent(tracer, trace)
        tracer.handle_message(
            AssistantMessage(
                model="m",
                content=[ToolUseBlock(id="c1", name="Read", input={})],
                usage={},
                parent_tool_use_id="agent1",
            )
        )
        child = tracer._open_tool_spans["agent1:c1"]
        # UserMessage with matching parent_id closes prefixed spans.
        tracer.handle_message(
            UserMessage(content="child result", parent_tool_use_id="agent1")
        )
        child.end.assert_called_once()
        assert "agent1:c1" not in tracer._open_tool_spans

    def test_agent_span_closed_with_tool_use_result(self, tracer, trace):
        agent_span, _ = self._open_agent(tracer, trace)
        tracer.handle_message(
            UserMessage(
                content=[ToolResultBlock(tool_use_id="agent1")],
                tool_use_result={
                    "content": [{"text": "agent done"}],
                    "agentId": "aid",
                    "agentType": "researcher",
                    "status": "completed",
                    "totalTokens": 999,
                },
            )
        )
        agent_span.end.assert_called_once()
        end_kwargs = agent_span.end.call_args.kwargs
        assert "agent done" in end_kwargs["output"]
        assert end_kwargs["metadata"]["agent_id"] == "aid"
        assert end_kwargs["metadata"]["total_tokens"] == 999
        assert "agent1" not in tracer._agent_spans


class TestFindAgentSpanId:
    """`_find_agent_span_id` must not misattribute under concurrent sub-agents."""

    def test_matches_by_explicit_tool_use_id(self, tracer):
        tracer._agent_spans["a1"] = MagicMock()
        tracer._agent_spans["a2"] = MagicMock()
        matched = tracer._find_agent_span_id([ToolResultBlock(tool_use_id="a2")])
        assert matched == "a2"

    def test_single_open_span_falls_back_when_no_id_match(self, tracer):
        # Unambiguous: exactly one open span, id doesn't match -> resolve to it.
        tracer._agent_spans["a1"] = MagicMock()
        matched = tracer._find_agent_span_id([ToolResultBlock(tool_use_id="nope")])
        assert matched == "a1"

    def test_two_open_spans_no_match_returns_none(self, tracer):
        # Ambiguous: two open spans, id matches neither -> must NOT guess.
        tracer._agent_spans["a1"] = MagicMock()
        tracer._agent_spans["a2"] = MagicMock()
        matched = tracer._find_agent_span_id([ToolResultBlock(tool_use_id="nope")])
        assert matched is None

    def test_two_open_spans_no_match_does_not_close_a_span(self, tracer):
        # Caller must not force-close an arbitrary span when attribution is ambiguous.
        span_a, span_b = MagicMock(), MagicMock()
        tracer._agent_spans["a1"] = span_a
        tracer._agent_spans["a2"] = span_b
        tracer._close_agent_span_with_result(
            UserMessage(content=[ToolResultBlock(tool_use_id="nope")]),
            {"content": [{"text": "done"}]},
        )
        span_a.end.assert_not_called()
        span_b.end.assert_not_called()
        # Both remain open; finalize will clean them up.
        assert set(tracer._agent_spans) == {"a1", "a2"}


class TestTaskLifecycle:
    def test_task_started_updates_agent_span(self, tracer):
        agent_span = MagicMock()
        tracer._agent_spans["agent1"] = agent_span
        tracer.handle_message(
            TaskStartedMessage(
                tool_use_id="agent1",
                data={"task_id": "T1", "subagent_type": "researcher", "task_type": "x"},
            )
        )
        agent_span.update.assert_called_once()
        assert agent_span.update.call_args.kwargs["metadata"]["task_id"] == "T1"

    def test_task_started_unknown_agent_is_noop(self, tracer):
        # No matching agent span -> nothing raised, nothing updated.
        tracer.handle_message(TaskStartedMessage(tool_use_id="nope", data={}))

    def test_task_progress_is_noop_but_safe(self, tracer):
        tracer.handle_message(
            TaskProgressMessage(
                data={"tool_use_id": "a", "description": "d", "last_tool_name": "Bash"}
            )
        )

    def test_task_notification_updates_status_and_usage(self, tracer):
        agent_span = MagicMock()
        tracer._agent_spans["agent1"] = agent_span
        tracer.handle_message(
            TaskNotificationMessage(
                tool_use_id="agent1",
                status="running",
                usage={"total_tokens": 50, "duration_ms": 12, "tool_uses": 3},
            )
        )
        meta = agent_span.update.call_args.kwargs["metadata"]
        assert meta["status"] == "running"
        assert meta["total_tokens"] == 50
        assert meta["tool_uses"] == 3


class TestFinalize:
    def test_closes_open_tool_and_agent_spans(self, tracer, trace, lf_client):
        tool_span = MagicMock()
        agent_span = MagicMock()
        tracer._open_tool_spans["t1"] = tool_span
        tracer._agent_spans["a1"] = agent_span
        tracer.model_name = "claude-opus"
        tracer.total_cost_usd = 1.5
        tracer.total_input_tokens = 100
        tracer.total_output_tokens = 200
        tracer.cache_read_tokens = 30
        tracer.output_text = "done"
        tracer._llm_call_count = 2

        tracer.finalize()

        tool_span.end.assert_called_once_with(output="(no result received)")
        agent_span.end.assert_called_once_with(output="(agent did not complete)")
        assert tracer._open_tool_spans == {}
        assert tracer._agent_spans == {}

        trace.update.assert_called_once()
        meta = trace.update.call_args.kwargs["metadata"]
        assert trace.update.call_args.kwargs["output"] == "done"
        assert meta["model"] == "claude-opus"
        assert meta["total_cost_usd"] == 1.5
        assert meta["cache_read_tokens"] == 30
        assert meta["llm_calls"] == 2
        assert meta["total_input_tokens"] == 100
        assert meta["total_output_tokens"] == 200
        lf_client.flush.assert_called_once()

    def test_finalize_with_no_open_spans(self, tracer, trace, lf_client):
        tracer.finalize()
        trace.update.assert_called_once()
        assert trace.update.call_args.kwargs["output"] is None
        lf_client.flush.assert_called_once()
