"""Shared resume-recall gate for the session-contract bed (docs/08 §B.1).

The canonical proof that "resume works" is **behavioral**, not structural: a
resumed session must give the *model* access to prior turns. So we plant a fact
in turn 1, close the session, resume the SAME id in a FRESH ``SessionManager``
(the crash-recovery proxy — empty in-memory map, shared on-disk index), ask the
model to recall the fact in turn 2, and assert the planted token comes back.

A green run proves S1–S4 for the provider in one shot:
  * S1 identity — the id captured in turn 1 is the id resumed in turn 2;
  * S2 registration — the row survived in the durable index;
  * S3 lifecycle — close() then resume() by id;
  * S4 resume = re-attach + MEMORY — the model recalled a turn-1 fact.

Everything runs through the real :class:`Orchestrator` (provider-agnostic), so
for codex this also exercises the message-handler path (bug 4b): zero events →
no reply text → no recall → FAIL.

Used by the three thin drivers ``{claude,codex,openharness}_session_smoke.py``.
"""

from __future__ import annotations

from pathlib import Path

from warden.orchestrator.orchestrator import Orchestrator
from warden.orchestrator.session.db import SessionDB
from warden.orchestrator.session.index import SessionIndex
from warden.orchestrator.session.manager import SessionManager
from warden.schemas.events import (
    CompletionEvent,
    ErrorEvent,
    MessageEvent,
    SessionCreatedEvent,
)

#: A distinctive, unpromptable token — the model would never volunteer it, so
#: its presence in turn 2 can ONLY come from reloaded turn-1 memory.
SECRET = "heliotrope"
PLANT = (
    f"Remember this exactly for later: my favorite color is {SECRET}. "
    "Just acknowledge with the single word: ok."
)
RECALL = "What is my favorite color? Answer with only the single color word."


async def drain_turn(
    orch: Orchestrator,
    *,
    provider: str,
    model: str | None,
    prompt: str,
    session_id: str | None,
) -> tuple[str | None, str, str | None]:
    """Run ONE turn through a PRE-BUILT orchestrator, draining its events.

    Returns ``(session_id, joined_reply_text, error_text)``. Shared by the
    resume-recall gate (persistence off) and the crash-recovery gate
    (persistence on) — the only difference between them is how the orchestrator
    is constructed, so the event loop lives here.
    """
    captured = session_id
    texts: list[str] = []
    err: str | None = None
    try:
        async for ev in orch.send_message(
            prompt, provider=provider, model=model, session_id=session_id,
        ):
            if isinstance(ev, SessionCreatedEvent):
                captured = ev.session_id or captured
            elif isinstance(ev, MessageEvent) and ev.kind in ("text", "stream_delta"):
                txt = ev.content.get("text", "")
                if txt:
                    texts.append(txt)
                    print(f"[{provider}][text] {txt.strip()[:200]}", flush=True)
            elif isinstance(ev, ErrorEvent):
                err = ev.text
                print(f"[{provider}][ERROR] {ev.text}", flush=True)
            elif isinstance(ev, CompletionEvent):
                captured = ev.session_id or captured
    finally:
        await orch.close()
    return captured, "".join(texts), err


async def _one_turn(
    *,
    manager: SessionManager,
    run_dir: Path,
    provider: str,
    model: str | None,
    prompt: str,
    session_id: str | None,
) -> tuple[str | None, str, str | None]:
    """Run ONE turn through a fresh (persistence-off) Orchestrator on ``manager``."""
    orch = Orchestrator(session_manager=manager, repo_path=run_dir)
    return await drain_turn(
        orch, provider=provider, model=model, prompt=prompt, session_id=session_id,
    )


async def run_recall_gate(
    provider: str,
    *,
    model: str | None = None,
    run_dir: Path | None = None,
) -> int:
    """Plant → close → resume (fresh manager) → recall. Returns 0 (PASS)/1 (FAIL).

    ``run_dir`` defaults to ``/work/run`` (git-initialized by the entrypoint so
    codex trusts it). Both managers share one on-disk index under ``run_dir`` —
    the fresh turn-2 manager therefore resumes purely off SQLite + the provider
    SDK's own transcript, exactly the cross-process crash-recovery path.
    """
    run_dir = run_dir or Path("/work/run")
    run_dir.mkdir(parents=True, exist_ok=True)
    db_path = run_dir / "session_recall.db"

    print("=" * 66)
    print(f" SESSION RESUME-RECALL GATE — provider={provider} "
          f"model={model or 'default'}")
    print(f" plant '{SECRET}' → close → resume (fresh manager) → recall")
    print("=" * 66)

    # --- Turn 1: plant the fact in manager A, then close the session ---------
    mgr_a = SessionManager(index=SessionIndex(SessionDB(db_path)))
    await mgr_a.init()
    try:
        sid, reply1, err1 = await _one_turn(
            manager=mgr_a, run_dir=run_dir, provider=provider,
            model=model, prompt=PLANT, session_id=None,
        )
        if sid:
            await mgr_a.close(sid)
    finally:
        await mgr_a.close_all()
        await mgr_a.close_index()

    print(f"\n  turn1: session_id={sid!r} reply={reply1.strip()[:80]!r} "
          f"err={err1!r}")
    if not sid:
        print("\n RESULT: FAIL — no session_id captured in turn 1 "
              "(the provider never streamed a first message).")
        return 1
    if err1:
        print("\n RESULT: FAIL — turn 1 errored (see above).")
        return 1

    # --- Turn 2: resume the SAME id in a BRAND-NEW manager, ask to recall ----
    mgr_b = SessionManager(index=SessionIndex(SessionDB(db_path)))
    await mgr_b.init()
    try:
        # S8 (transcript addressable) — report the persisted jsonl_path so all
        # three provider gates visibly confirm parity. Non-fatal: recall (S4) is
        # the gate; a null pointer here is a consistency note, not a failure.
        row = await mgr_b._index.get(sid)
        jsonl = row.get("jsonl_path") if row else None
        print(f"  index row: provider={row and row.get('provider')!r} "
              f"jsonl_path={jsonl!r} {'(S8 ✓)' if jsonl else '(S8 null)'}")
        if mgr_b.get(sid) is not None:
            print("\n RESULT: FAIL — fresh manager already holds the session "
                  "in memory (not a real cross-process resume).")
            return 1
        sid2, reply2, err2 = await _one_turn(
            manager=mgr_b, run_dir=run_dir, provider=provider,
            model=model, prompt=RECALL, session_id=sid,
        )
    finally:
        await mgr_b.close_all()
        await mgr_b.close_index()

    print(f"\n  turn2: resumed_id={sid2!r} reply={reply2.strip()[:120]!r} "
          f"err={err2!r}")

    recalled = SECRET.lower() in reply2.lower()
    same_id = sid2 == sid
    print("\n" + "=" * 66)
    if recalled and same_id and not err2:
        print(f" RESULT: PASS — resumed session {sid} recalled '{SECRET}'.")
        print(" S1–S4 proven: identity + registration + close/resume + MEMORY.")
        print("=" * 66)
        return 0

    print(" RESULT: FAIL —")
    if err2:
        print("   - turn 2 errored (resume/auth failure).")
    if not same_id:
        print(f"   - resumed id {sid2!r} != planted id {sid!r} "
              "(re-attach did not pin the id).")
    if not recalled:
        print(f"   - '{SECRET}' NOT in turn-2 reply → the model had NO "
              "cross-turn memory (resume restored an EMPTY conversation).")
    print("=" * 66)
    return 1
