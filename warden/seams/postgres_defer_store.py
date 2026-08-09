"""EXT-C3 — the Postgres-backed durable HITL defer store (multi-replica shared backend).

The file-backed :class:`~warden.seams.defer_store.FileDeferStore` persists a
parked tool call in a **run-scoped directory local to one container**. A fleet of
replicas behind a load balancer needs that pending/decision state in a **shared**
store: a run may PARK a call on replica A, be RESOLVED out-of-band, and RESUME on
replica B. This backend keeps the identical Protocol (:class:`DurableDeferStore`) +
status lifecycle (``pending → resolved → consumed``) but stores rows in Postgres, so
two PROCESSES running/resuming the SAME ``run_id`` share records.

**local backend is fine for a single container; Postgres is required to run more than
one replica.** (The single-container default stays :class:`FileDeferStore`; this is
selected only when ``state.backend == "postgres"``.)

Run isolation: :class:`FileDeferStore` is constructed per-run under a run-scoped root
(``.../hitl_defer/<run_id>``), so a ``content_key`` collision across two DIFFERENT runs
is impossible. This store preserves that by carrying ``run_id`` in the primary key AND
in EVERY WHERE clause, so two runs sharing the one ``hitl_defers`` table never see each
other's records — while two processes on the SAME ``run_id`` do.

Sync-only: the :class:`DurableDeferStore` Protocol is SYNCHRONOUS, so this uses the
synchronous **psycopg 3** driver (no event loop). ``psycopg`` is imported LAZILY and is
an OPTIONAL extra (``uv sync --extra postgres``), so this module imports WITHOUT the
driver installed and the hermetic suite never needs a DB. Autocommit is on — each verb
is its own tiny transaction, the simplest correct choice for a low-frequency HITL store.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from warden.seams.defer_store import (
    Decision,
    PendingRecord,
    content_key,
)


def _ck_digest(ck: str) -> str:
    """Stable NUL-free digest of a content_key for the TEXT column + index.

    ``content_key`` embeds a ``\\x00`` separator (fine for a filesystem hash, but
    Postgres TEXT rejects NUL bytes), and could be arbitrarily large. We store a
    sha256 hex digest instead — the lookup is exact-equality only, so hashing both
    the stored value and the probe preserves identical (tool_name, input) matching.
    """
    return hashlib.sha256(ck.encode()).hexdigest()

_PSYCOPG_HINT = (
    "PostgresDeferStore requires psycopg (v3). Install the optional extra: "
    "`uv sync --extra postgres` (or `pip install 'engines-harness[postgres]'`)."
)

# --- DDL: idempotent, mirrors the PendingRecord dataclass + run scoping -------

_DDL = """
CREATE TABLE IF NOT EXISTS hitl_defers (
    run_id        TEXT NOT NULL,
    tool_use_id   TEXT NOT NULL,
    tool_name     TEXT NOT NULL,
    tool_input    JSONB,
    session_id    TEXT,
    content_key   TEXT NOT NULL,
    status        TEXT NOT NULL,
    allow         BOOLEAN,
    updated_input JSONB,
    reason        TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, tool_use_id)
);
CREATE INDEX IF NOT EXISTS hitl_defers_content_idx
    ON hitl_defers (run_id, content_key);
"""


def _require_psycopg() -> Any:
    """Import ``psycopg`` (v3) lazily with a clear install hint (LAW 4: no silent failure)."""
    try:
        import psycopg  # noqa: PLC0415  (lazy by design — optional extra)
    except ImportError as exc:  # pragma: no cover - exercised on the bed
        raise ImportError(_PSYCOPG_HINT) from exc
    return psycopg


def _loads(value: Any) -> Any:
    """psycopg returns JSONB as already-decoded Python; be defensive if it's a str."""
    if isinstance(value, (str, bytes, bytearray)):
        return json.loads(value)
    return value


class PostgresDeferStore:
    """Postgres-backed :class:`DurableDeferStore`. Two instances over the same
    ``(dsn, run_id)`` (i.e. two processes) see the same records — that is the
    durability + cross-process contract. Different ``run_id`` values are isolated.

    Satisfies the SYNC Protocol with identical signatures + semantics to
    :class:`FileDeferStore`. Connect is LAZY (first use), so a routing/type check
    that never touches a verb needs no DB; the driver import IS eager (construction
    fails loudly if psycopg is absent).
    """

    def __init__(self, dsn: str | None, run_id: str) -> None:
        # Fail loudly at construction if the driver is absent (mirrors the ledger /
        # run registry): the import is lazy but eager-at-construct.
        _require_psycopg()
        if not dsn:
            raise ValueError(
                "PostgresDeferStore needs a DSN (state.dsn / WARDEN_POSTGRES_DSN) "
                "to connect — none was provided."
            )
        self._dsn = dsn
        self._run_id = run_id
        self._conn: Any = None

    # --- connection --------------------------------------------------------

    def _connection(self) -> Any:
        """Lazily open the autocommit connection + ensure the schema (idempotent)."""
        if self._conn is None:
            psycopg = _require_psycopg()
            self._conn = psycopg.connect(self._dsn, autocommit=True)
            self._conn.execute(_DDL)
        return self._conn

    def close(self) -> None:
        """Close the connection (teardown hook)."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # --- write side --------------------------------------------------------

    def record_pending(
        self, tool_use_id: str, tool_name: str, tool_input: dict,
        session_id: str | None,
    ) -> PendingRecord:
        """Persist a pending call (idempotent on ``(run_id, tool_use_id)``). Does not
        overwrite an already-``resolved``/``consumed`` record."""
        existing = self._read(tool_use_id)
        if existing is not None and existing.status != "pending":
            return existing
        ck = content_key(tool_name, tool_input)
        rec = PendingRecord(
            tool_use_id=tool_use_id, tool_name=tool_name, tool_input=tool_input,
            session_id=session_id, content_key=ck,
        )
        # ON CONFLICT DO NOTHING: a concurrent/idempotent re-record of the same
        # (run_id, tool_use_id) is a no-op; an existing pending row is left intact.
        self._connection().execute(
            "INSERT INTO hitl_defers "
            "(run_id, tool_use_id, tool_name, tool_input, session_id, content_key, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (run_id, tool_use_id) DO NOTHING",
            (
                self._run_id, tool_use_id, tool_name, json.dumps(tool_input),
                session_id, _ck_digest(ck), "pending",
            ),
        )
        return rec

    def resolve(
        self, tool_use_id: str, *, allow: bool,
        updated_input: dict | None = None, reason: str = "",
    ) -> bool:
        """The out-of-band approver writes a decision for a pending record.
        Returns False if the id is unknown (in this run)."""
        cur = self._connection().execute(
            "UPDATE hitl_defers "
            "SET status = %s, allow = %s, updated_input = %s, reason = %s "
            "WHERE run_id = %s AND tool_use_id = %s",
            (
                "resolved", allow,
                json.dumps(updated_input) if updated_input is not None else None,
                reason, self._run_id, tool_use_id,
            ),
        )
        return cur.rowcount > 0

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
            # Consume-once idempotency: only flip a still-resolved row to consumed.
            # rowcount==0 (someone else consumed it concurrently) ⇒ treat as gone.
            cur = self._connection().execute(
                "UPDATE hitl_defers SET status = %s "
                "WHERE run_id = %s AND tool_use_id = %s AND status = %s",
                ("consumed", self._run_id, rec.tool_use_id, "resolved"),
            )
            if cur.rowcount == 0:
                return None
        return Decision(
            allow=bool(rec.allow), updated_input=rec.updated_input, reason=rec.reason
        )

    def read_pending(self) -> list[PendingRecord]:
        """Enumerate parked (pending) records for THIS run only."""
        cur = self._connection().execute(
            "SELECT tool_use_id, tool_name, tool_input, session_id, "
            "status, allow, updated_input, reason "
            "FROM hitl_defers WHERE run_id = %s AND status = %s "
            "ORDER BY tool_use_id",
            (self._run_id, "pending"),
        )
        return [self._row_to_record(row) for row in cur.fetchall()]

    # --- internals ---------------------------------------------------------

    def _row_to_record(self, row: tuple) -> PendingRecord:
        (
            tool_use_id, tool_name, tool_input, session_id,
            status, allow, updated_input, reason,
        ) = row
        ti = _loads(tool_input)
        return PendingRecord(
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            tool_input=ti,
            session_id=session_id,
            # PendingRecord.content_key is the RAW key (the column stores its digest);
            # recompute it from the (tool_name, input) so callers see identical shape.
            content_key=content_key(tool_name, ti),
            status=status,
            allow=allow,
            updated_input=_loads(updated_input) if updated_input is not None else None,
            reason=reason or "",
        )

    def _read(self, tool_use_id: str | None) -> PendingRecord | None:
        if not tool_use_id:
            return None
        cur = self._connection().execute(
            "SELECT tool_use_id, tool_name, tool_input, session_id, "
            "status, allow, updated_input, reason "
            "FROM hitl_defers WHERE run_id = %s AND tool_use_id = %s",
            (self._run_id, tool_use_id),
        )
        row = cur.fetchone()
        return self._row_to_record(row) if row is not None else None

    def _read_by_content(self, ck: str) -> PendingRecord | None:
        cur = self._connection().execute(
            "SELECT tool_use_id, tool_name, tool_input, session_id, "
            "status, allow, updated_input, reason "
            "FROM hitl_defers WHERE run_id = %s AND content_key = %s "
            "ORDER BY tool_use_id LIMIT 1",
            (self._run_id, _ck_digest(ck)),
        )
        row = cur.fetchone()
        return self._row_to_record(row) if row is not None else None


__all__ = ["PostgresDeferStore"]
