"""Codex SDK auth smoke — a hello turn proves the injected credential authed.

In the clean Docker bed the ONLY codex credential present is the injected one
(a mounted ``CODEX_HOME/auth.json`` for OAuth, or an injected ``OPENAI_API_KEY``
for API-key mode). A completed hello turn therefore PROVES auth (C1/T1/T2) — a
false green is impossible by construction.

Runs the REAL CodexSdkSession directly (not the orchestrator) with a trivial
prompt capped to one turn.

    python -m warden.tests.e2e.codex_auth_smoke

Exit 0 = the turn completed (auth proven). Non-zero = auth failed / no reply.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from warden.providers.codex.sdk_session import CodexSdkSession


async def main() -> int:
    run_dir = Path("/tmp/codex_auth_smoke")
    run_dir.mkdir(parents=True, exist_ok=True)

    codex_home = os.environ.get("CODEX_HOME")
    api_key = os.environ.get("OPENAI_API_KEY")
    mode = "api-key" if api_key else ("oauth" if codex_home else "inherited")
    print("=" * 66)
    print(f" Codex SDK auth smoke — mode={mode} codex_home={codex_home}")
    print("=" * 66)

    # Fail-open callback (auth smoke does not test permissions).
    class _Allow:
        behavior = "allow"

    async def allow_cb(name, inp, ctx):
        return _Allow()

    auth_env = {"OPENAI_API_KEY": api_key} if api_key else None
    session = CodexSdkSession(
        repo_path=run_dir,
        can_use_tool=allow_cb,
        codex_home=Path(codex_home) if codex_home else None,
        auth_env=auth_env,
    )

    got_text = False
    try:
        await session.start()
        print(f"[auth] thread started: {session.session_id}")
        async for ev in session.send("Reply with exactly the word HELLO and nothing else."):
            kind = ev.get("kind") if isinstance(ev, dict) else None
            if kind == "text":
                text = ev.get("text", "")
                print(f"[auth][text] {text.strip()[:120]}")
                if text.strip():
                    got_text = True
            elif kind == "error":
                print(f"[auth][ERROR] {ev.get('text')}")
            elif kind == "status":
                print(f"[auth][usage] {ev.get('usage')}")
    finally:
        await session.close()

    print("=" * 66)
    if got_text and session.session_id:
        print(" CODEX AUTH SMOKE: PASS — hello turn completed, credential authed.")
        print("=" * 66)
        return 0
    print(" CODEX AUTH SMOKE: FAIL — no reply / no session id (auth failed?).")
    print("=" * 66)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
