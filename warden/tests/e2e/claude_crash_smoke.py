"""Claude crash-recovery bed gate (docs/08 §C.2, S6).

Thin driver over ``_session_crash``: persistence-active plant → snapshot → WIPE
the task workspace → restore from the snapshot → resume → recall. Proves the
session's memory survives a destroyed+restored workspace (the real crash /
stateless-worker path), not just a fresh manager on intact disk.

Run in-image via ``python -m warden.tests.e2e.claude_crash_smoke``
(the ``--claude-crash`` bed mode). Exits 0 on PASS, 1 on FAIL.
"""

from __future__ import annotations

import asyncio
import sys

from warden.tests.e2e._session_crash import run_crash_gate


if __name__ == "__main__":
    sys.exit(asyncio.run(run_crash_gate("claude")))
