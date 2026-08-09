"""Durable pending-approval store (pre-07b · durable mode) — the persistence that
lets a deferred tool call survive process death.

The warm :class:`~warden.seams.defer.DeferRegistry` holds the pending
call on an in-memory ``asyncio.Future`` — correct for interactive, seconds-long
approvals, but a liability when the approval may arrive minutes-to-days later
(a held coroutine pins memory and dies on any restart). The durable path instead
**persists the pending call, ejects it from memory, ends the turn**, and later
**rehydrates + injects** the decision — possibly in a *fresh process*.

:class:`DurableDeferStore` is the **Protocol** for that persistence (three verbs:
``record_pending`` / ``resolve`` / ``get_decision``). :class:`FileDeferStore` is
the tiny, file-backed impl (one JSON file per record) M6 ships on — deliberately
so, so the two-subprocess proof shares only the on-disk store, never memory. The
handler binds to the **Protocol**, so M6's later Postgres ``run_events``-backed
impl (``RunEventsDeferStore``) is a drop-in with **zero handler churn** (§3.0 of
``docs/improve_scope/new_tasks/07-durable-hitl.md``).

Keying: every record is addressable by BOTH the provider ``tool_use_id`` (exact,
Claude native-defer) AND a ``content_key`` (``tool_name`` + normalized input) so
the re-drive providers (OpenHarness / Codex), whose resumed call mints a *new*
id, can still find the decision. The stored ``tool_use_id`` remains the durable
record identity (what an approval API / inbox references).

Status lifecycle (idempotency): ``pending`` → ``resolved`` (approver wrote a
decision) → ``consumed`` (a resume injected it). ``get_decision`` returns a
resolved-but-unconsumed decision once and marks it consumed, so a duplicate
resume / replayed node is a no-op.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from warden.harness_api.config import HarnessApiConfig


def content_key(tool_name: str, tool_input: dict) -> str:
    """Stable content key for the re-drive/no-id path: a re-issued call has a
    new id but the same ``(tool_name, normalized input)``."""
    try:
        payload = json.dumps(tool_input, sort_keys=True, default=str)
    except Exception:
        payload = repr(tool_input)
    return f"{tool_name}\x00{payload}"


@dataclass
class Decision:
    allow: bool
    updated_input: dict | None = None
    reason: str = ""


@dataclass
class PendingRecord:
    tool_use_id: str
    tool_name: str
    tool_input: dict
    session_id: str | None
    content_key: str
    status: str = "pending"  # pending | resolved | consumed
    allow: bool | None = None
    updated_input: dict | None = None
    reason: str = ""


def _safe_name(key: str) -> str:
    """Filesystem-safe filename for an arbitrary key (id or content key)."""
    import hashlib

    return hashlib.sha256(key.encode()).hexdigest()[:32]


@runtime_checkable
class DurableDeferStore(Protocol):
    """The persistence contract M6's durable_http handler binds to (not any
    concrete impl), so the file store and a future ``run_events``/Postgres store
    are interchangeable. Three verbs + a read:

    - ``record_pending`` — persist a parked call (idempotent on ``tool_use_id``);
    - ``resolve`` — the out-of-band approver writes an allow/deny decision;
    - ``get_decision`` — the resume/inject step: return a resolved decision once
      (then mark it consumed — the idempotency guarantee), by exact id first then
      by content (the re-drive path);
    - ``read_pending`` — enumerate parked records (replay/inspection).
    """

    def record_pending(
        self, tool_use_id: str, tool_name: str, tool_input: dict,
        session_id: str | None,
    ) -> "PendingRecord": ...

    def resolve(
        self, tool_use_id: str, *, allow: bool,
        updated_input: dict | None = None, reason: str = "",
    ) -> bool: ...

    def get_decision(
        self, tool_use_id: str | None, tool_name: str, tool_input: dict,
        *, consume: bool = True,
    ) -> "Decision | None": ...

    def read_pending(self) -> "list[PendingRecord]": ...


class FileDeferStore:
    """File-backed :class:`DurableDeferStore` impl. Two instances over the same
    ``root`` (i.e. two processes) see the same records — that is the durability
    contract. This is the impl M6 ships on; the Postgres ``run_events`` impl is a
    later drop-in behind the same Protocol.

    Layout under ``root``::

        records/<hash(tool_use_id)>.json   # the canonical record
        by_content/<hash(content_key)>     # a pointer file → tool_use_id
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._records = self._root / "records"
        self._by_content = self._root / "by_content"
        self._records.mkdir(parents=True, exist_ok=True)
        self._by_content.mkdir(parents=True, exist_ok=True)

    # --- write side --------------------------------------------------------

    def record_pending(
        self, tool_use_id: str, tool_name: str, tool_input: dict,
        session_id: str | None,
    ) -> PendingRecord:
        """Persist a pending call (idempotent on ``tool_use_id``). Does not
        overwrite an already-``resolved``/``consumed`` record."""
        existing = self._read(tool_use_id)
        if existing is not None and existing.status != "pending":
            return existing
        ck = content_key(tool_name, tool_input)
        rec = PendingRecord(
            tool_use_id=tool_use_id, tool_name=tool_name, tool_input=tool_input,
            session_id=session_id, content_key=ck,
        )
        self._write(rec)
        (self._by_content / _safe_name(ck)).write_text(tool_use_id)
        return rec

    def resolve(
        self, tool_use_id: str, *, allow: bool,
        updated_input: dict | None = None, reason: str = "",
    ) -> bool:
        """The out-of-band approver writes a decision for a pending record.
        Returns False if the id is unknown."""
        rec = self._read(tool_use_id)
        if rec is None:
            return False
        rec.status = "resolved"
        rec.allow = allow
        rec.updated_input = updated_input
        rec.reason = reason
        self._write(rec)
        return True

    # --- read side (resume/inject) ----------------------------------------

    def get_decision(
        self, tool_use_id: str | None, tool_name: str, tool_input: dict,
        *, consume: bool = True,
    ) -> Decision | None:
        """Return a resolved decision for this call, by exact id first then by
        content (the re-drive path). Marks it ``consumed`` once (idempotency)."""
        rec = self._read(tool_use_id) if tool_use_id else None
        if rec is None:
            rec = self._read_by_content(content_key(tool_name, tool_input))
        if rec is None or rec.status != "resolved":
            return None
        if consume:
            rec.status = "consumed"
            self._write(rec)
        return Decision(allow=bool(rec.allow), updated_input=rec.updated_input, reason=rec.reason)

    def read_pending(self) -> list[PendingRecord]:
        out: list[PendingRecord] = []
        for p in sorted(self._records.glob("*.json")):
            try:
                out.append(PendingRecord(**json.loads(p.read_text())))
            except Exception:
                continue
        return out

    # --- internals ---------------------------------------------------------

    def _path(self, tool_use_id: str) -> Path:
        return self._records / f"{_safe_name(tool_use_id)}.json"

    def _write(self, rec: PendingRecord) -> None:
        self._path(rec.tool_use_id).write_text(json.dumps(asdict(rec), default=str))

    def _read(self, tool_use_id: str | None) -> PendingRecord | None:
        if not tool_use_id:
            return None
        p = self._path(tool_use_id)
        if not p.exists():
            return None
        try:
            return PendingRecord(**json.loads(p.read_text()))
        except Exception:
            return None

    def _read_by_content(self, ck: str) -> PendingRecord | None:
        ptr = self._by_content / _safe_name(ck)
        if not ptr.exists():
            return None
        return self._read(ptr.read_text().strip())


# --- tier switch -------------------------------------------------------------


def build_defer_store(
    cfg: "HarnessApiConfig", run_id: str, local_root: Path | str
) -> DurableDeferStore:
    """Select the durable defer store from the shared-state tier switch (EXT-C3).

    local backend is fine for a single container; Postgres is required to run more
    than one replica. When ``cfg.state.is_postgres`` the store is the shared
    ``PostgresDeferStore(dsn, run_id)`` (two processes on the same ``run_id`` share
    records; different runs stay isolated); otherwise the process-local
    :class:`FileDeferStore` over the run-scoped ``local_root``.
    """
    if cfg.state.is_postgres:
        # Lazy import: keep the file-store default path free of the Postgres module
        # (and its optional driver) — mirrors build_run_registry.
        from warden.seams.postgres_defer_store import PostgresDeferStore

        return PostgresDeferStore(dsn=cfg.state.dsn, run_id=run_id)
    return FileDeferStore(local_root)
