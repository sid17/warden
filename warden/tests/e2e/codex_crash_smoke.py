"""Codex crash-recovery bed gate (docs/08 §C.2, S6).

Thin driver over ``_session_crash``: persistence-active plant → snapshot → WIPE
the task workspace → restore from the snapshot → resume → recall. Under
persistence CODEX_HOME is pinned to ``<task>/.codex``; the helper seeds the
OAuth auth.json there so the persisted turn authenticates and the rollout is
snapshotted (see ``_session_crash`` for the credential caveat).

Run in-image via ``python -m warden.tests.e2e.codex_crash_smoke``
(the ``--codex-crash`` bed mode, OAuth). Exits 0 on PASS, 1 on FAIL.
"""

from __future__ import annotations

import asyncio
import sys

from warden.tests.e2e._session_crash import run_crash_gate


if __name__ == "__main__":
    sys.exit(asyncio.run(run_crash_gate("codex")))
