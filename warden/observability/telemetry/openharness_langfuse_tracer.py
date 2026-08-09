"""Langfuse trace manager for OpenHarness sessions.

Handles observation lifecycle: traces, generations, and tool spans
for OpenHarness StreamEvents. Agent tool spans are held open until
the subprocess finishes, then enriched with real output via
BackgroundTaskManager completion listeners.
"""

import logging
import re
from typing import Any

from warden.observability.telemetry import get_langfuse, truncate_output
from warden.schemas import semconv

logger = logging.getLogger(__name__)

_TASK_ID_RE = re.compile(r"\btask_id=([a-zA-Z0-9]+)")


def _parse_agent_task_id(output: str) -> str | None:
    """Extract task_id from agent spawn confirmation string.

    Expected format: "Spawned agent agent@default (task_id=a871a9eae, backend=subprocess)"
    Returns the task_id or None if the pattern doesn't match.
    """
    m = _TASK_ID_RE.search(output)
    return m.group(1) if m else None


class OpenHarnessLangfuseTracer:
    """Manages Langfuse trace lifecycle for a single OpenHarness send() call.

    Maps StreamEvents to Langfuse observations:
        AssistantTurnComplete   → generation (one per LLM turn)
        ToolExecutionStarted    → span open
        ToolExecutionCompleted  → span close with output
    """

    def __init__(self, trace: Any, lf_client: Any, model: str):
        self._trace = trace
        self._lf = lf_client
        self._model = model
        self._turn_count = 0
        self._open_tool_span: Any = None
        self._open_tool_name: str = ""
        self._final_output: str = ""
        # Agent spans held open until subprocess completes
        self._pending_agent_spans: dict[str, Any] = {}
        self._unregister_fns: list[Any] = []

    @classmethod
    def create(
        cls, session_id: str | None, prompt: str, model: str, cfg=None,
    ) -> "OpenHarnessLangfuseTracer | None":
        lf = get_langfuse(cfg)
        if not lf:
            return None
        trace = lf.trace(
            name="openharness.interaction",
            session_id=session_id, input=prompt,
            metadata={"provider": "openharness", "model": model},
        )
        return cls(trace, lf, model)

    def handle_event(self, event: Any) -> None:
        etype = type(event).__name__
        dispatch = {
            "AssistantTurnComplete": lambda: self._handle_turn_complete(event),
            "ToolExecutionStarted": lambda: self._handle_tool_started(event),
            "ToolExecutionCompleted": lambda: self._handle_tool_completed(event),
        }
        handler = dispatch.get(etype)
        if handler:
            handler()

    def set_final_output(self, text: str) -> None:
        """Track the last non-empty assistant response as trace output."""
        if text:
            self._final_output = text

    def _handle_turn_complete(self, event: Any) -> None:
        self._turn_count += 1
        usage = getattr(event, "usage", None)
        in_tok = getattr(usage, "input_tokens", 0) if usage else 0
        out_tok = getattr(usage, "output_tokens", 0) if usage else 0

        # The assistant text is accumulated by session.py and passed
        # via set_final_output(). We read the turn's text from there
        # but the generation output is set by the caller after accumulation.

        from langfuse.model import ModelUsage
        self._trace.generation(
            name=f"llm_call_{self._turn_count}",
            model=self._model,
            output=self._final_output or None,
            usage=ModelUsage(
                input=in_tok, output=out_tok,
                total=in_tok + out_tok, unit="TOKENS",
            ),
            metadata={
                "turn": self._turn_count,
                **semconv.usage_attrs(
                    {"input_tokens": in_tok, "output_tokens": out_tok},
                    model=self._model,
                ),
            },
        )

    def _handle_tool_started(self, event: Any) -> None:
        tool_name = getattr(event, "tool_name", "unknown")
        tool_input = getattr(event, "tool_input", {})
        self._open_tool_name = tool_name
        self._open_tool_span = self._trace.span(
            name=f"tool: {tool_name}",
            input=tool_input,
        )

    def _handle_tool_completed(self, event: Any) -> None:
        if not self._open_tool_span:
            return
        tool_output = getattr(event, "output", "")
        is_error = getattr(event, "is_error", False)

        # Agent tool: hold span open for async enrichment
        if self._open_tool_name == "agent":
            task_id = _parse_agent_task_id(tool_output)
            if task_id:
                self._pending_agent_spans[task_id] = self._open_tool_span
                logger.info("Agent span held open for task_id=%s", task_id)
                self._open_tool_span = None
                self._open_tool_name = ""
                return
            # No task_id found — close normally (unexpected format)
            logger.warning("Agent tool output missing task_id: %s", tool_output[:100])

        self._open_tool_span.end(
            output=truncate_output(tool_output),
            metadata={"is_error": is_error},
        )
        self._open_tool_span = None
        self._open_tool_name = ""

    def register_agent_completion_listeners(self) -> None:
        """Register completion listeners for all pending agent spans.

        For each held-open agent span, registers a callback with
        BackgroundTaskManager that closes the span with real output
        when the subprocess finishes. Also checks if the task already
        completed (fast agents).
        """
        if not self._pending_agent_spans:
            return

        try:
            from openharness.tasks.manager import get_task_manager
            manager = get_task_manager()
        except (ImportError, Exception):
            logger.warning("Cannot import BackgroundTaskManager — closing agent spans without enrichment")
            self._close_all_pending("(task manager unavailable)")
            return

        for task_id, span in list(self._pending_agent_spans.items()):
            # Check if already completed (fast agent)
            task = manager.get_task(task_id)
            if task and task.status in ("completed", "failed", "killed"):
                self._close_agent_span_with_task(task_id, task, manager)
                continue

            # Register async listener for when the subprocess finishes
            lf = self._lf

            def _make_listener(tid: str, s: Any) -> Any:
                async def _on_complete(task_record: Any) -> None:
                    try:
                        output = manager.read_task_output(tid)
                        duration_ms = 0
                        if task_record.ended_at and task_record.created_at:
                            duration_ms = int(
                                (task_record.ended_at - task_record.created_at) * 1000
                            )
                        s.end(
                            output=truncate_output(output),
                            metadata={
                                "status": task_record.status,
                                "return_code": task_record.return_code,
                                "duration_ms": duration_ms,
                                # OBS-3 boundary-only: sub-agent internals run
                                # in an OpenHarness-owned child subprocess we
                                # can't instrument this cycle — mark the boundary
                                # so an empty span isn't read as "nothing done".
                                "internals_captured": False,
                            },
                        )
                        lf.flush()
                        logger.info(
                            "Agent span closed via listener: task_id=%s status=%s",
                            tid, task_record.status,
                        )
                    except Exception:
                        logger.exception("Error closing agent span for task_id=%s", tid)
                return _on_complete

            unregister = manager.register_completion_listener(
                _make_listener(task_id, span)
            )
            self._unregister_fns.append(unregister)
            logger.info("Registered completion listener for task_id=%s", task_id)

    def _close_agent_span_with_task(
        self, task_id: str, task: Any, manager: Any,
    ) -> None:
        """Close an agent span using data from a completed TaskRecord."""
        span = self._pending_agent_spans.pop(task_id, None)
        if not span:
            return
        output = manager.read_task_output(task_id)
        duration_ms = 0
        if task.ended_at and task.created_at:
            duration_ms = int((task.ended_at - task.created_at) * 1000)
        span.end(
            output=truncate_output(output),
            metadata={
                "status": task.status,
                "return_code": task.return_code,
                "duration_ms": duration_ms,
                "internals_captured": False,
            },
        )
        logger.info(
            "Agent span closed immediately: task_id=%s status=%s",
            task_id, task.status,
        )

    def _close_all_pending(self, fallback_output: str) -> None:
        """Close all pending agent spans with a fallback message."""
        for task_id, span in self._pending_agent_spans.items():
            span.end(
                output=fallback_output,
                metadata={"status": "unknown", "internals_captured": False},
            )
            logger.info("Agent span closed with fallback: task_id=%s", task_id)
        self._pending_agent_spans.clear()

    def finalize(self) -> None:
        # Close any unclosed tool span
        if self._open_tool_span:
            self._open_tool_span.end(output="(no result received)")
            self._open_tool_span = None

        # Best-effort close for agent spans still pending
        if self._pending_agent_spans:
            try:
                from openharness.tasks.manager import get_task_manager
                manager = get_task_manager()
                for task_id in list(self._pending_agent_spans):
                    task = manager.get_task(task_id)
                    if task and task.status in ("completed", "failed", "killed"):
                        self._close_agent_span_with_task(task_id, task, manager)
                    else:
                        span = self._pending_agent_spans.pop(task_id)
                        span.end(
                            output="(agent still running)",
                            metadata={"status": "running", "internals_captured": False},
                        )
                        logger.info(
                            "Agent span closed as still-running: task_id=%s", task_id
                        )
            except (ImportError, Exception):
                self._close_all_pending("(agent still running)")

        self._trace.update(
            output=self._final_output or None,
            metadata={
                "provider": "openharness",
                "model": self._model,
                "llm_calls": self._turn_count,
            },
        )
        self._lf.flush()
