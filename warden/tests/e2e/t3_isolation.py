"""T3 — Claude SDK per-run auth isolation (C2/T3) + AUTH-FIX regression.

The open questions this answers, end-to-end, inside the Docker bed:

  (1) C2/T3 no-bleed: two ``ClaudeSession``s constructed in ONE process, each
      carrying a DIFFERENT injected key, must NOT bleed each other's credential.
      After ``start()`` each session's ``options.env`` (built by the real
      start path) must carry ONLY its own injected key — no trace of the other
      session's key and no inherited Claude credential from ``os.environ``.

  (2) AUTH-FIX regression: with ``ANTHROPIC_AUTH_TOKEN`` set in ``os.environ``
      (an inherited bearer token that used to shadow an injected key), a session
      injecting a DIFFERENT credential must strip the inherited bearer first —
      the injected key wins, the inherited AUTH_TOKEN is gone.

Structural proof (no API spend, no real turn): we drive the REAL ``start()``
path, which builds ``options.env`` through ``BaseProvider.apply_auth_env``, and
inspect the resulting env on the live SDK client's options. This is the exact
env the SDK child would inherit, so a pass here is the no-bleed guarantee. The
orchestrator may additionally run a real 2-key turn leg if a second real key is
available; this driver is the structural gate.

Run inside the Docker bed:
    python -m warden.tests.e2e.t3_isolation

Exit 0 = T3 PASS (no bleed + AUTH-FIX holds). Non-zero = FAIL.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from warden.providers.claude.session import ClaudeSession


def _env_after_start(sess: ClaudeSession) -> dict:
    """Return the options.env the SDK client was constructed with."""
    client = sess._client
    assert client is not None, "session not started (no SDK client)"
    options = client.options
    return dict(options.env or {})


async def _build_env(auth_env: dict[str, str]) -> dict:
    """Construct + start a ClaudeSession with the given injected auth, then
    return its resolved options.env. Uses a throwaway repo dir.

    We run the REAL ``start()`` env-building path (build_claude_otel_env +
    BaseProvider.apply_auth_env + CLAUDE_CONFIG_DIR pin) but neutralize the
    actual SDK ``connect()`` so the check does not depend on the (deliberately
    bogus, isolated) injected keys authenticating a real child process. The env
    on the constructed client's ``options`` is the exact env the child would
    inherit, so inspecting it is the faithful no-bleed proof.
    """
    from claude_agent_sdk import ClaudeSDKClient

    run_dir = Path("/tmp/t3_isolation")
    run_dir.mkdir(parents=True, exist_ok=True)

    orig_connect = ClaudeSDKClient.connect

    async def _noop_connect(self, *args, **kwargs):  # noqa: ANN001
        return None

    ClaudeSDKClient.connect = _noop_connect  # type: ignore[assignment]
    try:
        sess = ClaudeSession(repo_path=run_dir, auth_env=auth_env)
        await sess.start()
        return _env_after_start(sess)
    finally:
        ClaudeSDKClient.connect = orig_connect  # type: ignore[assignment]


async def main() -> int:
    print("=" * 66)
    print(" T3 — Claude SDK per-run auth isolation (C2) + AUTH-FIX regression")
    print("=" * 66)

    # Seed os.environ with an inherited bearer token that MUST be stripped.
    os.environ["ANTHROPIC_AUTH_TOKEN"] = "inherited-bearer-should-be-stripped"

    key_a = "sk-ant-session-A-only"
    key_b = "sk-ant-session-B-only"

    env_a = await _build_env({"ANTHROPIC_API_KEY": key_a})
    env_b = await _build_env({"ANTHROPIC_API_KEY": key_b})

    # (1) No-bleed: each session carries only its own key.
    a_ok = (
        env_a.get("ANTHROPIC_API_KEY") == key_a
        and key_b not in env_a.values()
    )
    b_ok = (
        env_b.get("ANTHROPIC_API_KEY") == key_b
        and key_a not in env_b.values()
    )
    print(f"  session A env has A-key only : {a_ok}")
    print(f"  session B env has B-key only : {b_ok}")

    # (2) AUTH-FIX: the inherited bearer token was stripped from both.
    authfix_ok = (
        "ANTHROPIC_AUTH_TOKEN" not in env_a
        and "ANTHROPIC_AUTH_TOKEN" not in env_b
    )
    print(f"  inherited ANTHROPIC_AUTH_TOKEN stripped : {authfix_ok}")

    # No inherited CLAUDE_CODE_OAUTH_TOKEN leaked either (defensive).
    no_oauth_bleed = (
        "CLAUDE_CODE_OAUTH_TOKEN" not in env_a
        and "CLAUDE_CODE_OAUTH_TOKEN" not in env_b
    )
    print(f"  no inherited OAuth token bleed          : {no_oauth_bleed}")

    print()
    print("=" * 66)
    if a_ok and b_ok and authfix_ok and no_oauth_bleed:
        print(" T3 RESULT: PASS — per-run keys are isolated (no bleed) and the")
        print(" inherited AUTH_TOKEN is stripped (AUTH-FIX holds).")
        print("=" * 66)
        return 0

    print(" T3 RESULT: FAIL")
    if not (a_ok and b_ok):
        print("   - KEY BLEED: a session's env carried the other session's key.")
    if not authfix_ok:
        print("   - AUTH-FIX REGRESSION: inherited ANTHROPIC_AUTH_TOKEN survived.")
    if not no_oauth_bleed:
        print("   - inherited CLAUDE_CODE_OAUTH_TOKEN survived the strip.")
    print("=" * 66)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
