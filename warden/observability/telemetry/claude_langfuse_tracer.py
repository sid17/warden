"""Langfuse trace manager for Claude SDK sessions.

Handles observation lifecycle: traces, generations, tool spans,
and sub-agent nesting via parent_tool_use_id routing.
"""

import logging
from typing import Any

from warden.observability.telemetry import get_langfuse, truncate_output
from warden.schemas import semconv

logger = logging.getLogger(__name__)


def _get_field(msg: Any, field: str, fallback_data: dict | None = None):
    """Get a field from msg attrs, falling back to msg.data dict."""
    val = getattr(msg, field, None)
    if val is not None:
        return val
    if fallback_data:
        return fallback_data.get(field)
    return None


class ClaudeLangfuseTracer:
    """Manages Langfuse trace lifecycle for a single send() call.

    Routes sub-agent messages under their parent Agent span using
    the parent_tool_use_id field from SDK messages.
    """

    def __init__(self, trace: Any, lf_client: Any):
        self._trace = trace
        self._lf = lf_client
        self._open_tool_spans: dict[str, Any] = {}  # key → span
        self._agent_spans: dict[str, Any] = {}  # tool_use_id → span
        self._llm_call_count = 0
        self.model_name: str | None = None
        self.total_cost_usd: float | None = None
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.cache_read_tokens: int = 0
        self.output_text: str = ""

    @classmethod
    def create(cls, session_id: str | None, prompt: str, cfg=None) -> "ClaudeLangfuseTracer | None":
        lf = get_langfuse(cfg)
        if not lf:
            return None
        trace = lf.trace(
            name="claude_code.interaction",
            session_id=session_id, input=prompt,
            metadata={"provider": "claude"},
        )
        return cls(trace, lf)

    def update_session_id(self, session_id: str) -> None:
        self._trace.update(session_id=session_id)

    def handle_message(self, msg: Any) -> None:
        msg_type = type(msg).__name__
        parent_id = getattr(msg, "parent_tool_use_id", None)
        dispatch = {
            "UserMessage": lambda: self._handle_user_message(msg, parent_id),
            "AssistantMessage": lambda: self._handle_assistant_message(msg, parent_id),
            "TaskStartedMessage": lambda: self._handle_task_started(msg),
            "TaskProgressMessage": lambda: self._handle_task_progress(msg),
            "TaskNotificationMessage": lambda: self._handle_task_notification(msg),
            "ResultMessage": lambda: self._handle_result(msg),
        }
        handler = dispatch.get(msg_type)
        if handler:
            handler()

    # -- UserMessage: close tool spans with results --

    def _handle_user_message(self, msg: Any, parent_id: str | None) -> None:
        content = getattr(msg, "content", None)
        tool_use_result = getattr(msg, "tool_use_result", None)

        if tool_use_result and parent_id is None:
            self._close_agent_span_with_result(msg, tool_use_result)
        elif parent_id and parent_id in self._agent_spans:
            self._close_spans_by_prefix(f"{parent_id}:", content)
        elif self._open_tool_spans:
            result = truncate_output(self._extract_result_text(content))
            main_keys = [k for k in self._open_tool_spans if ":" not in k]
            for k in main_keys:
                self._open_tool_spans.pop(k).end(output=result)

    def _close_spans_by_prefix(self, prefix: str, content: Any) -> None:
        result = truncate_output(self._extract_result_text(content))
        keys = [k for k in self._open_tool_spans if k.startswith(prefix)]
        for k in keys:
            self._open_tool_spans.pop(k).end(output=result)

    def _close_agent_span_with_result(self, msg: Any, tool_use_result: dict) -> None:
        content = getattr(msg, "content", None)
        matched_id = self._find_agent_span_id(content)
        if not matched_id:
            return
        agent_span = self._agent_spans.pop(matched_id)
        result_text = self._extract_result_text(tool_use_result.get("content", []))
        agent_span.end(
            output=truncate_output(result_text),
            metadata={
                "agent_id": tool_use_result.get("agentId"),
                "agent_type": tool_use_result.get("agentType"),
                "status": tool_use_result.get("status"),
                "total_duration_ms": tool_use_result.get("totalDurationMs"),
                "total_tokens": tool_use_result.get("totalTokens"),
                "tool_use_count": tool_use_result.get("totalToolUseCount"),
            },
        )

    def _find_agent_span_id(self, content: Any) -> str | None:
        """Find which agent span matches the ToolResultBlock in content."""
        if isinstance(content, list):
            for item in content:
                if type(item).__name__ == "ToolResultBlock":
                    tid = getattr(item, "tool_use_id", None)
                elif isinstance(item, dict):
                    tid = item.get("tool_use_id")
                else:
                    continue
                if tid and tid in self._agent_spans:
                    return tid
        # No explicit id match. Only fall back when exactly ONE agent span is
        # open (unambiguous); with 2+ concurrent sub-agents, guessing would
        # misattribute telemetry, so return None and let finalize() clean up.
        if len(self._agent_spans) == 1:
            return next(iter(self._agent_spans))
        return None

    # -- AssistantMessage: create generations and tool spans --

    def _handle_assistant_message(self, msg: Any, parent_id: str | None) -> None:
        model = getattr(msg, "model", None)
        content = getattr(msg, "content", None) or []
        usage = getattr(msg, "usage", None)

        # Determine observation parent
        if parent_id and parent_id in self._agent_spans:
            obs_parent = self._agent_spans[parent_id]
        else:
            obs_parent = self._trace
            if model:
                self.model_name = model

        # Close stale tool spans for this context
        if parent_id and parent_id in self._agent_spans:
            self._close_stale_spans(f"{parent_id}:")
        elif not parent_id:
            self._close_stale_spans(None)

        # Token usage
        call_input, call_output = 0, 0
        if isinstance(usage, dict):
            call_input = usage.get("input_tokens", 0)
            call_output = usage.get("output_tokens", 0)
            if not parent_id:
                self.total_input_tokens += call_input
                self.total_output_tokens += call_output
                self.cache_read_tokens += usage.get("cache_read_input_tokens", 0)

        # Parse content blocks
        tool_blocks, output_parts = self._parse_content_blocks(content, parent_id)

        # Create Langfuse generation
        if isinstance(content, list):
            self._create_generation(
                obs_parent, msg, model, content,
                call_input, call_output, output_parts, parent_id,
            )
            self._open_tool_spans_for_blocks(obs_parent, tool_blocks, parent_id)

    def _close_stale_spans(self, prefix: str | None) -> None:
        """Close stale tool spans matching prefix (None = main-level, no colon)."""
        if prefix:
            keys = [k for k in self._open_tool_spans if k.startswith(prefix)]
        else:
            keys = [k for k in self._open_tool_spans if ":" not in k]
        for k in keys:
            self._open_tool_spans.pop(k).end(output=truncate_output(None))

    def _parse_content_blocks(
        self, content: list, parent_id: str | None
    ) -> tuple[list[Any], list[str]]:
        tool_blocks: list[Any] = []
        output_parts: list[str] = []
        if not isinstance(content, list):
            return tool_blocks, output_parts
        for block in content:
            btype = type(block).__name__
            text = getattr(block, "text", None)
            if text:
                if not parent_id:
                    self.output_text += text
                output_parts.append(text)
            elif btype == "ThinkingBlock":
                thinking = getattr(block, "thinking", "") or ""
                output_parts.append(f"[thinking] {thinking[:500] or '(empty)'}")
            elif btype in ("ToolUseBlock", "ServerToolUseBlock"):
                tool_blocks.append(block)
                name = getattr(block, "name", "?")
                tag = "server_tool" if btype == "ServerToolUseBlock" else "tool_use"
                output_parts.append(f"[{tag}] {name}({str(getattr(block, 'input', {}))[:200]})")
            elif btype == "ServerToolResultBlock":
                tid = getattr(block, "tool_use_id", None)
                rc = getattr(block, "content", {})
                span = self._open_tool_spans.pop(tid, None)
                if span:
                    span.end(output=str(rc)[:500])
                output_parts.append(f"[server_result] {str(rc)[:100]}")
        return tool_blocks, output_parts

    def _create_generation(
        self, obs_parent: Any, msg: Any, model: str | None,
        content: list, call_input: int, call_output: int,
        output_parts: list[str], parent_id: str | None,
    ) -> None:
        from langfuse.model import ModelUsage
        self._llm_call_count += 1
        meta: dict[str, Any] = {"content_blocks": [type(b).__name__ for b in content]}
        stop_reason = getattr(msg, "stop_reason", None)
        message_id = getattr(msg, "message_id", None)
        if stop_reason:
            meta["stop_reason"] = stop_reason
        if message_id:
            meta["message_id"] = message_id
        if parent_id:
            meta["parent_tool_use_id"] = parent_id
        meta.update(semconv.usage_attrs(
            {"input_tokens": call_input, "output_tokens": call_output},
            model=model or self.model_name,
            system="anthropic",
        ))
        obs_parent.generation(
            name=f"llm_call_{self._llm_call_count}",
            model=model or self.model_name,
            output="\n".join(output_parts) if output_parts else None,
            usage=ModelUsage(
                input=call_input, output=call_output,
                total=call_input + call_output, unit="TOKENS",
            ),
            metadata=meta,
        )

    def _open_tool_spans_for_blocks(
        self, obs_parent: Any, tool_blocks: list[Any], parent_id: str | None,
    ) -> None:
        for block in tool_blocks:
            tool_id = getattr(block, "id", None)
            tool_name = getattr(block, "name", "unknown")
            tool_input = getattr(block, "input", {})
            if tool_name == "Agent" and tool_id:
                self._agent_spans[tool_id] = obs_parent.span(
                    name="tool: Agent", input=tool_input,
                    metadata={
                        "tool_call_id": tool_id,
                        "description": tool_input.get("description", ""),
                        "subagent_type": tool_input.get("subagent_type", ""),
                    },
                )
            else:
                key = f"{parent_id}:{tool_id}" if parent_id else tool_id
                if key:
                    self._open_tool_spans[key] = obs_parent.span(
                        name=f"tool: {tool_name}", input=tool_input,
                        metadata={"tool_call_id": tool_id},
                    )

    # -- Task lifecycle (sub-agent management) --

    def _handle_task_started(self, msg: Any) -> None:
        data = getattr(msg, "data", {}) or {}
        tid = _get_field(msg, "tool_use_id", data)
        if tid and tid in self._agent_spans:
            self._agent_spans[tid].update(metadata={
                "task_id": _get_field(msg, "task_id", data),
                "subagent_type": _get_field(msg, "subagent_type", data),
                "task_type": _get_field(msg, "task_type", data),
            })

    def _handle_task_progress(self, msg: Any) -> None:
        data = getattr(msg, "data", {}) or {}
        logger.debug(
            "TaskProgress: %s — %s (last_tool=%s)",
            _get_field(msg, "tool_use_id", data),
            _get_field(msg, "description", data),
            _get_field(msg, "last_tool_name", data),
        )

    def _handle_task_notification(self, msg: Any) -> None:
        data = getattr(msg, "data", {}) or {}
        tid = _get_field(msg, "tool_use_id", data)
        status = _get_field(msg, "status", data)
        usage = getattr(msg, "usage", None) or data.get("usage", {})
        if tid and tid in self._agent_spans:
            usage_meta = {}
            if isinstance(usage, dict):
                usage_meta = {
                    "total_tokens": usage.get("total_tokens"),
                    "duration_ms": usage.get("duration_ms"),
                    "tool_uses": usage.get("tool_uses"),
                }
            self._agent_spans[tid].update(metadata={"status": status, **usage_meta})

    # -- ResultMessage --

    def _handle_result(self, msg: Any) -> None:
        result_text = getattr(msg, "result", None)
        if result_text and not self.output_text:
            self.output_text = result_text
        self.total_cost_usd = getattr(msg, "total_cost_usd", None)
        metadata: dict[str, Any] = {}
        for field in ("model_usage", "duration_ms", "duration_api_ms", "num_turns"):
            val = getattr(msg, field, None)
            if val is not None:
                metadata[field] = val
        if metadata:
            self._trace.update(metadata=metadata)

    # -- Finalization --

    def finalize(self) -> None:
        for span in self._open_tool_spans.values():
            span.end(output="(no result received)")
        self._open_tool_spans.clear()
        for span in self._agent_spans.values():
            span.end(output="(agent did not complete)")
        self._agent_spans.clear()
        self._trace.update(
            output=self.output_text or None,
            metadata={
                "provider": "claude", "model": self.model_name,
                "total_cost_usd": self.total_cost_usd,
                "cache_read_tokens": self.cache_read_tokens,
                "llm_calls": self._llm_call_count,
                "total_input_tokens": self.total_input_tokens,
                "total_output_tokens": self.total_output_tokens,
            },
        )
        self._lf.flush()

    # -- Helpers --

    @staticmethod
    def _extract_result_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(item.get("text", "") or item.get("content", "") or str(item)[:200])
                else:
                    text = getattr(item, "content", None)
                    parts.append(text if isinstance(text, str) else str(text or item)[:200])
            return "\n".join(parts)
        return str(content) if content is not None else ""
