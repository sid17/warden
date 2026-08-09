"""E6 — three-mode gate (approve / reject / revise) unit tests.

Covers the wire contract (``ToolConfirmation`` validation) and the ``_execute``
continuation selection in isolation from the full pause/resume loop:

- ``revise`` requires non-empty ``feedback`` (empty/absent → 422 at the route);
- ``approve``/``reject`` never require it;
- ``last_decision`` "revise"/"reject"/"approve" → the matching continuation string
  (revise interpolates the operator feedback).

The end-to-end revise loop (a second pause with a different proposal → approve →
converge, plus the §3c storm-stop) lives in ``test_runs_durable_hitl.py``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from warden.harness_api.runner import (
    _DURABLE_RESUME_CONTINUATION,
    _DURABLE_RESUME_DENIED,
    _DURABLE_RESUME_REVISE,
)
from warden.harness_api.schemas import ToolConfirmation


# --- the wire contract: revise requires feedback ------------------------------

def test_revise_with_feedback_validates():
    c = ToolConfirmation(tool_use_id="t", decision="revise", feedback="add a chapter")
    assert c.decision == "revise"
    assert c.feedback == "add a chapter"


def test_revise_with_empty_feedback_is_rejected():
    with pytest.raises(ValidationError, match="revise"):
        ToolConfirmation(tool_use_id="t", decision="revise", feedback="")


def test_revise_with_whitespace_feedback_is_rejected():
    with pytest.raises(ValidationError, match="revise"):
        ToolConfirmation(tool_use_id="t", decision="revise", feedback="   ")


def test_revise_with_absent_feedback_is_rejected():
    with pytest.raises(ValidationError, match="revise"):
        ToolConfirmation(tool_use_id="t", decision="revise")


def test_approve_needs_no_feedback():
    c = ToolConfirmation(tool_use_id="t", decision="approve")
    assert c.feedback is None


def test_reject_needs_no_feedback_but_may_carry_reason():
    c = ToolConfirmation(tool_use_id="t", decision="reject", reason="wrong concepts")
    assert c.feedback is None and c.reason == "wrong concepts"


def test_old_allow_deny_values_are_rejected():
    # The hard rename: the legacy wire enum no longer validates.
    for legacy in ("allow", "deny"):
        with pytest.raises(ValidationError):
            ToolConfirmation(tool_use_id="t", decision=legacy)


# --- the continuation selection (mirrors _execute's 3-way branch) -------------

def _select_continuation(last_decision: str | None, feedback: str | None) -> str:
    """The exact branch ``_execute`` uses to pick the decision-aware continuation."""
    if last_decision == "revise":
        return _DURABLE_RESUME_REVISE.format(feedback=feedback or "")
    if last_decision == "reject":
        return _DURABLE_RESUME_DENIED
    return _DURABLE_RESUME_CONTINUATION


def test_continuation_for_revise_interpolates_feedback():
    out = _select_continuation("revise", "add a chapter on X")
    assert "add a chapter on X" in out
    assert "MUST differ" in out
    assert "do not resubmit the same proposal" in out


def test_continuation_for_reject_is_the_denied_string():
    assert _select_continuation("reject", None) == _DURABLE_RESUME_DENIED


def test_continuation_for_approve_is_the_plain_continuation():
    assert _select_continuation("approve", None) == _DURABLE_RESUME_CONTINUATION
