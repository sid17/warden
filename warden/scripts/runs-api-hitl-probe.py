#!/usr/bin/env python3
"""M6 host probe — durable HITL over the Runs API (single-cell convenience CLI).

A thin host wrapper over the canonical gate logic in
``warden.tests.e2e.m6_hitl_smoke`` (which is also the in-image bed gate,
``docker/run.sh --m6-hitl``). Use this to drive ONE cell on the host without Docker
— same real Runner + real provider subprocess over the actual Runs-API routes (ASGI
in-process): ``POST /runs`` → poll ``requires_action`` → read the
``permission_request`` from ``/history`` → ``POST /tool_confirmation`` → poll
``succeeded``, with a marker/``out.txt`` check for whether the deferred tool ran.

Per the 07b split: durable_http is **Claude-only** (exact-id native defer +
neutral-continuation resume, multi-tool convergent). OpenHarness/Codex durable_http
is **fail-closed** — the probe expects the run to be REJECTED (ends ``error`` naming
the split), and drives no model there. Codex custom is N/A (ungated). Auth inherits
the process credential (OAuth Claude, free Ollama OpenHarness) — never the API-key lane.
Export the Claude OAuth token and strip the API key (as ``durable-defer-probe.py``):

    OAUTH=...  # from Keychain
    env -u ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN=$OAUTH PYTHONPATH=. \\
      uv run --no-sync python warden/scripts/runs-api-hitl-probe.py \\
      --provider claude --tool builtin --case allow --base /tmp/m6probe
  # custom: --tool custom ; OpenHarness: --provider openharness --model qwen3:8b ;
  # Codex: env -u OPENAI_API_KEY ... --provider codex --tool builtin

For the FULL matrix (all cells, PASS/FAIL verdict), use the gate module directly:
    python -m warden.tests.e2e.m6_hitl_smoke claude builtin,custom allow,deny,idempotent,sla
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
from pathlib import Path

from warden.tests.e2e.m6_hitl_smoke import _expected_ran, run_case


def _verdict(r: dict, case: str) -> str:
    # 07b: OH/Codex durable_http is fail-closed — the expected result is a rejection
    # (run ends error naming the split), NOT a pause/resume.
    if r.get("final_status") == "error" and "durable_http" in (r.get("error") or ""):
        return "PASS (fail-closed): durable_http rejected for this provider (07b)"
    mechanics = r["paused"] and r["resumed"]
    outcome = r["tool_ran"] == _expected_ran(case)
    if not mechanics:
        return (f"FAIL (mechanics): paused={r['paused']} resumed={r['resumed']} "
                f"final={r.get('final_status')} — never paused/resumed")
    if outcome:
        return "PASS: pause→confirm→resume + tool-ran outcome matches"
    return ("PARTIAL: paused+resumed OK but tool_ran="
            f"{r['tool_ran']} != expected {_expected_ran(case)} "
            "(on Claude this is a FAIL — investigate convergence)")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True,
                    choices=["claude", "openharness", "codex"])
    ap.add_argument("--model", default=None)
    ap.add_argument("--tool", default="builtin", choices=["builtin", "custom"])
    ap.add_argument("--case", required=True,
                    choices=["allow", "deny", "idempotent", "sla"])
    ap.add_argument("--base", required=True)
    args = ap.parse_args()

    if args.provider == "codex" and args.tool == "custom":
        print("SKIP: Codex custom tools are ungated by design (pre-07 §3d) — N/A.")
        return

    run_dir = Path(args.base).resolve() / f"{args.provider}-{args.tool}-{args.case}"
    shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*74}\nM6 RUNS-API HITL PROBE  provider={args.provider} "
          f"tool={args.tool} case={args.case}\n{'='*74}")
    r = await run_case(args.provider, args.model, run_dir, args.tool, args.case)
    for line in r.get("trace", []):
        print(f"  · {line}")
    print(f"\nVERDICT [{args.provider}/{args.tool}/{args.case}]: {_verdict(r, args.case)}\n")


if __name__ == "__main__":
    asyncio.run(main())
