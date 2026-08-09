"""OpenHarness resume-recall bed gate (docs/08 Part C.2, S1–S4).

Thin driver over ``_session_recall``: plant a fact, close, resume the same id in
a fresh manager, assert the model recalls it. This gate is what forces the
cold-resume fix (bug B-OH): until ``start()`` seeds the engine from the
transcript on resume, turn 2 runs on an EMPTY conversation and recall FAILs.

Runs against the host's free Ollama (``qwen3:8b`` by default via
``OPENHARNESS_MODEL``/``OPENHARNESS_BASE_URL``), never a cloud key. Run in-image
via ``python -m warden.tests.e2e.openharness_session_smoke`` (the
``--openharness-session`` bed mode). Exits 0 on PASS, 1 on FAIL.
"""

from __future__ import annotations

import asyncio
import os
import sys

from warden.tests.e2e._session_recall import run_recall_gate


if __name__ == "__main__":
    # Default to the tool-reliable local model unless the bed overrides it.
    os.environ.setdefault("OPENHARNESS_MODEL", "qwen3:8b")
    sys.exit(asyncio.run(run_recall_gate("openharness")))
