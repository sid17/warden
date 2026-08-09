"""Claude SDK resume-recall bed gate (docs/08 Part C.2, S1–S4).

Thin driver: plant a fact, close, resume the same id in a fresh manager, and
assert the model recalls it. All logic lives in ``_session_recall`` so the three
provider gates stay in lock-step. Claude has NO session bug — a green here
verifies the contract + the harness itself before Codex/OpenHarness.

Run in-image via ``python -m warden.tests.e2e.claude_session_smoke``
(the ``--claude-session`` bed mode). Exits 0 on PASS, 1 on FAIL.
"""

from __future__ import annotations

import asyncio
import sys

from warden.tests.e2e._session_recall import run_recall_gate


if __name__ == "__main__":
    sys.exit(asyncio.run(run_recall_gate("claude")))
