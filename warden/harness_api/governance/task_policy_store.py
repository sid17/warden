"""M2 3g.1 — a durable, append-only per-task governance policy store.

Caps layer as **run > task > tier** (:func:`~warden.harness_api.governance.policy.resolve_policy`).
The *tier* default is an operator constant and the *run* override is per-request, but
the *task* layer — a per-``task_id`` override — must be durable so it survives a
restart. :class:`JsonlTaskPolicyStore` is the DEFAULT durable backing for that layer:
an append-only JSONL file, event-sourced, folded into an in-memory
``task_id → GovernancePolicy`` map on :meth:`load`.

Read-mostly: :meth:`register` / :meth:`remove` append + update under a lock; the hot
path is a sync :meth:`get` off the in-memory map. Same crash-safety invariant as
:class:`~warden.harness_api.governance.jsonl_ledger.JsonlBalanceLedger` — the
file is never rewritten, so a crash can only lose the last partial line.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from warden.harness_api.governance.policy import GovernancePolicy

logger = logging.getLogger(__name__)


def _policy_to_dict(policy: GovernancePolicy) -> dict:
    """Serialize a policy to its three (None-safe) fields."""
    return {
        "cost_cap_usd": policy.cost_cap_usd,
        "deadline_s": policy.deadline_s,
        "max_turns": policy.max_turns,
    }


def _policy_from_dict(data: dict) -> GovernancePolicy:
    """Deserialize a policy from its three (None-safe) fields."""
    return GovernancePolicy(
        cost_cap_usd=data.get("cost_cap_usd"),
        deadline_s=data.get("deadline_s"),
        max_turns=data.get("max_turns"),
    )


class JsonlTaskPolicyStore:
    """Durable per-``task_id`` :class:`GovernancePolicy` overrides (the *task* layer).

    Append-only ``register`` / ``remove`` events fold into an in-memory map: a later
    ``register`` overrides an earlier one; a ``remove`` deletes the key.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._policies: dict[str, GovernancePolicy] = {}

    async def load(self) -> None:
        """Replay the JSONL file, folding events into memory (idempotent).

        Missing file ⇒ an empty store. A corrupt/partial last line is logged and
        skipped; all prior events still fold.
        """
        async with self._lock:
            self._policies = {}
            if not self._path.exists():
                return
            with self._path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except (ValueError, json.JSONDecodeError):
                        logger.warning(
                            "JsonlTaskPolicyStore: skipping corrupt line in %s: %r",
                            self._path,
                            line[:200],
                        )
                        continue
                    self._apply(event)

    def _apply(self, event: dict) -> None:
        """Fold one already-parsed event into the in-memory map."""
        etype = event.get("type")
        task_id = event.get("task_id")
        if task_id is None:
            return
        if etype == "register":
            policy_data = event.get("policy") or {}
            self._policies[task_id] = _policy_from_dict(policy_data)
        elif etype == "remove":
            self._policies.pop(task_id, None)
        # Unknown event types are ignored (forward-compat).

    def _append(self, event: dict) -> None:
        """Append one event as a JSON line, creating parent dirs on first write."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")

    async def register(self, task_id: str, policy: GovernancePolicy) -> None:
        """Register (or overwrite) the policy override for a task. Append + fold."""
        async with self._lock:
            event = {
                "type": "register",
                "task_id": task_id,
                "policy": _policy_to_dict(policy),
                "ts": time.time(),
            }
            self._append(event)
            self._apply(event)

    async def remove(self, task_id: str) -> None:
        """Remove a task's policy override (no-op if absent). Append + fold."""
        async with self._lock:
            event = {
                "type": "remove",
                "task_id": task_id,
                "ts": time.time(),
            }
            self._append(event)
            self._apply(event)

    def get(self, task_id: str) -> GovernancePolicy | None:
        """The current policy override for a task, or ``None`` (sync map read)."""
        return self._policies.get(task_id)


__all__ = ["JsonlTaskPolicyStore"]
