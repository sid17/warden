from warden.schemas import semconv
from warden.schemas.audit import AuditEvent
from warden.schemas.usage import Usage


def test_usage_attrs_from_usage_and_dict():
    a = semconv.usage_attrs(Usage(input=10, output=5), model="claude-opus-4-8")
    assert a["gen_ai.usage.input_tokens"] == 10
    assert a["gen_ai.usage.output_tokens"] == 5
    assert a["gen_ai.request.model"] == "claude-opus-4-8"
    assert a["gen_ai.operation.name"] == "chat"
    # raw provider dict shape also works
    b = semconv.usage_attrs({"input_tokens": 3, "output_tokens": 2})
    assert b["gen_ai.usage.input_tokens"] == 3


def test_tool_attrs():
    a = semconv.tool_attrs("Bash")
    assert a["gen_ai.tool.name"] == "Bash"
    assert a["gen_ai.operation.name"] == "execute_tool"


def test_semconv_names_match_audit_convention():
    # The audit JSONL derives gen_ai.* dot-notation from field names; our
    # explicit constants must be the SAME strings (one convention, no fork).
    ev = AuditEvent(
        event_type="PreToolUse", timestamp="t", run_id="r", session_id="s",
        gen_ai_tool_name="Bash", gen_ai_operation_name="execute_tool",
    )
    d = ev.to_jsonl_dict()
    assert semconv.TOOL_NAME in d
    assert semconv.OPERATION_NAME in d
