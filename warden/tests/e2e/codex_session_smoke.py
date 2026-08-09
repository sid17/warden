"""Codex SDK resume-recall bed gate (docs/08 Part C.2, S1–S4).

Thin driver over ``_session_recall``: plant a fact, close, resume the same
thread id in a fresh manager, assert the model recalls it. Driving through the
Orchestrator also exercises the codex message-handler path (bug 4b) — a broken
handler emits zero events, so there is no reply text and recall FAILs.

Run in-image via ``python -m warden.tests.e2e.codex_session_smoke``
(the ``--codex-session`` bed mode, OAuth). Exits 0 on PASS, 1 on FAIL.
"""

from __future__ import annotations

import asyncio
import sys

from warden.tests.e2e._session_recall import run_recall_gate


if __name__ == "__main__":
    sys.exit(asyncio.run(run_recall_gate("codex")))
