"""Mock-local durable event log — reuses the real ``RunEventLog`` storage, widens
only the replay row→model construction.

The real ``RunEventLog`` (imported verbatim for its schema + ``append`` + WAL setup)
rebuilds each replayed row into the STABLE ``harness_api.schemas.Event``, whose
``type`` Literal rejects ``permission_request``/``permission_resolved`` — so replaying
a gated run would raise. This subclass overrides ONLY ``replay`` to build the mock's
widened ``contract.Event`` (identical shape, merged ``type``). Everything else — the
table, ``append`` (append is duck-typed on attributes, so it already stores a widened
Event), ``last_seq``, ``init``/``close`` — is inherited unchanged.
"""

from __future__ import annotations

import json

from warden.harness_api.event_log import RunEventLog

from warden.harness_api_mock.contract import Event


class MockRunEventLog(RunEventLog):
    """``RunEventLog`` whose ``replay`` yields the mock's widened ``Event``."""

    async def replay(self, run_id: str, after_seq: int = 0) -> list[Event]:
        assert self._conn is not None, "MockRunEventLog.init() not called"
        cur = await self._conn.execute(
            "SELECT run_id, seq, type, session_id, data, at FROM run_events "
            "WHERE run_id = ? AND seq > ? ORDER BY seq ASC",
            (run_id, after_seq),
        )
        rows = await cur.fetchall()
        return [
            Event(
                run_id=r[0],
                seq=r[1],
                type=r[2],
                session_id=r[3],
                data=json.loads(r[4]),
                at=r[5],
            )
            for r in rows
        ]
