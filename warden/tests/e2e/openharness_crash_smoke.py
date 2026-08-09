"""OpenHarness crash-recovery bed gate (docs/08 §C.2, S6).

Thin driver over ``_session_crash``: persistence-active plant → snapshot → WIPE
the task workspace → restore from the snapshot → resume → recall, against the
host's free Ollama. Exercises the openharness home-pinning fix — its transcript
must live at ``<task>/.openharness`` (inside the snapshot) or a wiped task dir
would lose it.

Run in-image via ``python -m warden.tests.e2e.openharness_crash_smoke``
(the ``--openharness-crash`` bed mode). Exits 0 on PASS, 1 on FAIL.
"""

from __future__ import annotations

import asyncio
import os
import sys

from warden.tests.e2e._session_crash import run_crash_gate


if __name__ == "__main__":
    os.environ.setdefault("OPENHARNESS_MODEL", "qwen3:8b")
    sys.exit(asyncio.run(run_crash_gate("openharness")))
