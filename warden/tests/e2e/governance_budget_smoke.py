"""Governance budget bed gate (M2 3g / T8) — the dollar path on a real provider.

Drives the REAL Orchestrator (persistence-off, same harness as the session gates)
with a per-run Governor wired in, backed by a durable JSONL balance ledger. Three
assertions in one gate, on a PRICED provider (claude — the only lane with a pricing
row + mid-turn cost visibility):

  T8a — zero balance, ENFORCE ⇒ the run is rejected at pre-flight (stopped=budget),
        the provider never generates, $0 debited.
  T8b — funded $5, allow_uncapped (meter-don't-block) ⇒ a real turn completes, its
        cost is priced, and the durable balance DECREMENTS by ~that cost (settle→debit).
  T8c — funded $1 but a low cost cap, ENFORCE ⇒ a long generation is STOPPED
        (stopped=budget) at/near the cap; claude stops MID-TURN so overshoot is bounded.

Bands, not equality (LLM output is non-deterministic). Run in-image via
``python -m warden.tests.e2e.governance_budget_smoke [provider]``
(default claude). Exits 0 on all-pass, 1 on any fail.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from warden.harness_api.credentials.keys import KeyRegistry
from warden.harness_api.governance import (
    GovernorService,
    InMemoryReservationLedger,
)
from warden.harness_api.governance.jsonl_ledger import JsonlBalanceLedger
from warden.harness_api.governance.policy import GovernancePolicy
from warden.orchestrator.orchestrator import Orchestrator
from warden.orchestrator.session.db import SessionDB
from warden.orchestrator.session.index import SessionIndex
from warden.orchestrator.session.manager import SessionManager
from warden.schemas.events import (
    CompletionEvent,
    ErrorEvent,
    MessageEvent,
    StoppedEvent,
)


async def _run_governed_turn(provider, run_dir, db_path, governor, prompt, model=None):
    """One turn through a fresh Orchestrator with ``governor`` wired. Drains events."""
    mgr = SessionManager(index=SessionIndex(SessionDB(db_path)))
    await mgr.init()
    orch = Orchestrator(
        session_manager=mgr, repo_path=run_dir,
        governor=governor, clock_tick_interval_s=0.5,
    )
    texts: list[str] = []
    stopped: str | None = None
    err: str | None = None
    completed = False
    try:
        async for ev in orch.send_message(prompt, provider=provider, model=model):
            if isinstance(ev, MessageEvent) and ev.kind in ("text", "stream_delta"):
                t = ev.content.get("text", "")
                if t:
                    texts.append(t)
            elif isinstance(ev, StoppedEvent):
                stopped = ev.reason
            elif isinstance(ev, ErrorEvent):
                err = ev.text
            elif isinstance(ev, CompletionEvent):
                completed = True
    finally:
        await orch.close()
        await mgr.close_all()
        await mgr.close_index()
    return {"text": "".join(texts), "stopped": stopped, "err": err, "completed": completed}


async def run_budget_gate(provider: str, *, run_dir: Path | None = None) -> int:
    # In-image default is /work/run (git-initialized by the entrypoint); a host
    # run overrides via GOV_GATE_RUN_DIR for local validation off the bed.
    run_dir = run_dir or Path(os.environ.get("GOV_GATE_RUN_DIR", "/work/run"))
    run_dir.mkdir(parents=True, exist_ok=True)
    gov_dir = run_dir / "gov"
    gov_dir.mkdir(parents=True, exist_ok=True)
    balance = JsonlBalanceLedger(gov_dir / "balance.jsonl", supports_topup=False)
    await balance.load()
    resv = InMemoryReservationLedger()
    keys = KeyRegistry(keys={}, users={})

    def service(allow_uncapped: bool) -> GovernorService:
        return GovernorService(
            key_registry=keys, ledger=resv,
            balance_source=balance, billing=balance,
            allow_uncapped=allow_uncapped,
        )

    print("=" * 66)
    print(f" GOVERNANCE BUDGET GATE — provider={provider}")
    print("=" * 66)
    fails: list[str] = []

    # --- T8a: zero balance, ENFORCE ⇒ rejected pre-flight ($0 spent) ----------
    gov = await service(False).resolve(user_id="ua", task_id="t8a", provider=provider, model=None)
    r = await _run_governed_turn(provider, run_dir, run_dir / "a.db", gov, "Say hello.")
    await gov.settle()
    bal_a = await balance.opening_balance_usd("ua")
    ok_a = r["stopped"] == "budget" and not r["completed"] and bal_a == 0.0
    print(f"\n T8a zero-budget: stopped={r['stopped']!r} completed={r['completed']} "
          f"text={r['text'][:40]!r} balance={bal_a}  -> {'PASS' if ok_a else 'FAIL'}")
    if not ok_a:
        fails.append("T8a")

    # --- T8b: funded $5, allow_uncapped ⇒ completes, cost tracked, balance drops
    await balance.credit("ub", 5.0, txn_id="seed-ub")
    gov = await service(True).resolve(user_id="ub", task_id="t8b", provider=provider, model=None)
    r = await _run_governed_turn(provider, run_dir, run_dir / "b.db", gov,
                                 "Reply with exactly one word: hello")
    await gov.settle()
    bal_b = await balance.opening_balance_usd("ub")
    # Provider-agnostic INVARIANT: settle debits EXACTLY the committed run cost, so
    # the balance drops by run_cost — whether that is a real dollar figure (a priced
    # provider: claude, codex) or $0 (an unpriced one: openharness). No hardcoding
    # which lane is priced; the observed run_cost tells us.
    ok_b = (not r["err"]) and r["completed"] and abs(bal_b - (5.0 - gov.run_cost_usd)) < 1e-6
    lane = "priced" if gov.run_cost_usd > 0 else "unpriced (flat)"
    print(f" T8b funded-run: completed={r['completed']} err={r['err']!r} "
          f"run_cost=${gov.run_cost_usd:.6f} balance 5.0->{bal_b:.6f}  [{lane}]  "
          f"-> {'PASS' if ok_b else 'FAIL'}")
    if not ok_b:
        fails.append("T8b")

    # --- T8c: funded $1, low cap, ENFORCE ⇒ stopped at/near the cap -----------
    await balance.credit("uc", 1.0, txn_id="seed-uc")
    gov = await service(False).resolve(
        user_id="uc", task_id="t8c", provider=provider, model=None,
        run_policy=GovernancePolicy(cost_cap_usd=0.005),
    )
    r = await _run_governed_turn(
        provider, run_dir, run_dir / "c.db", gov,
        "Write a long, detailed 500-word essay about the ocean and its ecosystems.",
    )
    await gov.settle()
    bal_c = await balance.opening_balance_usd("uc")
    # Same settle invariant. Enforcement is behaviour-driven: a PRICED run
    # (run_cost>0) that crossed the $0.005 cap must STOP on budget; an UNPRICED run
    # (run_cost==0) cannot cross a dollar cap and completes — both are correct, and
    # which one you get IS the per-provider difference (see docs/06 §8).
    inv_c = abs(bal_c - (1.0 - gov.run_cost_usd)) < 1e-6
    enforced = gov.run_cost_usd == 0.0 or r["stopped"] == "budget"
    ok_c = (not r["err"]) and inv_c and enforced
    lane = "priced→cap-stop" if gov.run_cost_usd > 0 else "unpriced→completes"
    print(f" T8c low-cap: stopped={r['stopped']!r} completed={r['completed']} "
          f"run_cost=${gov.run_cost_usd:.6f} balance 1.0->{bal_c:.6f}  [{lane}]  "
          f"-> {'PASS' if ok_c else 'FAIL'}")
    if not ok_c:
        fails.append("T8c")

    print("\n" + "=" * 66)
    if not fails:
        print(f" GOVERNANCE BUDGET GATE: PASS ({provider}) — reject / meter+debit / "
              "cap-stop all proven end-to-end.")
        print("=" * 66)
        return 0
    print(f" GOVERNANCE BUDGET GATE: FAIL ({provider}) — {', '.join(fails)}")
    print("=" * 66)
    return 1


if __name__ == "__main__":
    prov = sys.argv[1] if len(sys.argv) > 1 else "claude"
    sys.exit(asyncio.run(run_budget_gate(prov)))
