"""SQLite state store for workflow runs and step results."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_name   TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    trigger_type    TEXT NOT NULL DEFAULT 'manual',
    started_at      TEXT,
    finished_at     TEXT,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS step_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES runs(id),
    step_id         TEXT NOT NULL,
    agent_name      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    prompt_rendered TEXT,
    result_text     TEXT,
    started_at      TEXT,
    finished_at     TEXT,
    error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_workflow ON runs(workflow_name);
CREATE INDEX IF NOT EXISTS idx_step_runs_run ON step_runs(run_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    """Async SQLite store for workflow execution state."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._db: aiosqlite.Connection | None = None

    async def init_db(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Call init_db() first"
        return self._db

    # ----- runs ----------------------------------------------------------

    async def create_run(self, workflow_name: str, trigger_type: str = "manual") -> int:
        cur = await self.db.execute(
            "INSERT INTO runs (workflow_name, status, trigger_type, started_at) VALUES (?, 'running', ?, ?)",
            (workflow_name, trigger_type, _now()),
        )
        await self.db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def update_run(self, run_id: int, *, status: str, error: str | None = None) -> None:
        await self.db.execute(
            "UPDATE runs SET status = ?, finished_at = ?, error = ? WHERE id = ?",
            (status, _now(), error, run_id),
        )
        await self.db.commit()

    async def get_run(self, run_id: int) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_runs(self, workflow_name: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        if workflow_name:
            cur = await self.db.execute(
                "SELECT * FROM runs WHERE workflow_name = ? ORDER BY id DESC LIMIT ?",
                (workflow_name, limit),
            )
        else:
            cur = await self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]

    # ----- step_runs ----------------------------------------------------

    async def create_step_run(
        self,
        run_id: int,
        step_id: str,
        agent_name: str,
        prompt_rendered: str = "",
    ) -> int:
        cur = await self.db.execute(
            "INSERT INTO step_runs (run_id, step_id, agent_name, status, prompt_rendered, started_at) "
            "VALUES (?, ?, ?, 'running', ?, ?)",
            (run_id, step_id, agent_name, prompt_rendered, _now()),
        )
        await self.db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def update_step_run(
        self,
        step_run_id: int,
        *,
        status: str,
        result_text: str | None = None,
        error: str | None = None,
    ) -> None:
        await self.db.execute(
            "UPDATE step_runs SET status = ?, result_text = ?, finished_at = ?, error = ? WHERE id = ?",
            (status, result_text, _now(), error, step_run_id),
        )
        await self.db.commit()

    async def get_step_results(self, run_id: int) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT * FROM step_runs WHERE run_id = ? ORDER BY id",
            (run_id,),
        )
        return [dict(r) for r in await cur.fetchall()]
