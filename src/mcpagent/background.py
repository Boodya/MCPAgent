"""Background workflow manager — non-blocking execution from interactive chat."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcpagent.workflow_engine import RunResult, WorkflowEngine
    from mcpagent.workflow_models import WorkflowLoader

log = logging.getLogger(__name__)


@dataclass
class BackgroundEvent:
    """Completion event pushed to the CLI for proactive notification."""

    task_id: str
    workflow_name: str
    status: str  # "completed" | "failed"
    summary: str  # Human-readable result summary


@dataclass
class BackgroundTask:
    """Metadata for a running or finished background workflow."""

    id: str
    workflow_name: str
    status: str  # "running" | "completed" | "failed"
    started_at: datetime
    finished_at: datetime | None = None
    result: RunResult | None = None
    error: str | None = None
    _asyncio_task: asyncio.Task | None = field(default=None, repr=False)


class BackgroundManager:
    """Manages background workflow execution and delivers completion events.

    Usage::

        bg = BackgroundManager(engine, loader)
        task_id = bg.submit("my-workflow")   # non-blocking
        ...
        event = await bg.events.get()        # wait for completion
    """

    def __init__(self, engine: WorkflowEngine, loader: WorkflowLoader) -> None:
        self._engine = engine
        self._loader = loader
        self._tasks: dict[str, BackgroundTask] = {}
        self.events: asyncio.Queue[BackgroundEvent] = asyncio.Queue()
        self._counter = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_workflow_names(self) -> list[str]:
        """Return names of all available workflows."""
        return [wf.name for wf in self._loader.load_all()]

    def submit(self, workflow_name: str) -> str:
        """Start a workflow in the background. Returns a task ID immediately."""
        workflows = {wf.name: wf for wf in self._loader.load_all()}
        wf = workflows.get(workflow_name)
        if not wf:
            available = list(workflows.keys())
            raise ValueError(
                f"Workflow '{workflow_name}' not found. Available: {available}"
            )

        self._counter += 1
        task_id = f"bg-{self._counter}"
        now = datetime.now(timezone.utc)

        task = BackgroundTask(
            id=task_id,
            workflow_name=workflow_name,
            status="running",
            started_at=now,
        )
        task._asyncio_task = asyncio.create_task(
            self._run(task_id, wf),
            name=f"bg-workflow-{workflow_name}",
        )
        self._tasks[task_id] = task
        log.info("Submitted background workflow '%s' as %s", workflow_name, task_id)
        return task_id

    def get_tasks(self, task_id: str | None = None) -> list[BackgroundTask]:
        """Return task info. If *task_id* given, return only that task."""
        if task_id:
            t = self._tasks.get(task_id)
            return [t] if t else []
        return list(self._tasks.values())

    def cancel(self, task_id: str) -> bool:
        """Cancel a running background task."""
        task = self._tasks.get(task_id)
        if not task or task.status != "running":
            return False
        if task._asyncio_task and not task._asyncio_task.done():
            task._asyncio_task.cancel()
            task.status = "cancelled"
            task.finished_at = datetime.now(timezone.utc)
            log.info("Cancelled background task %s", task_id)
            return True
        return False

    async def shutdown(self) -> None:
        """Cancel all running tasks (called on app exit)."""
        for task in self._tasks.values():
            if task._asyncio_task and not task._asyncio_task.done():
                task._asyncio_task.cancel()
        # Wait briefly for cancellations to propagate
        running = [
            t._asyncio_task
            for t in self._tasks.values()
            if t._asyncio_task and not t._asyncio_task.done()
        ]
        if running:
            await asyncio.gather(*running, return_exceptions=True)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run(self, task_id: str, wf) -> None:  # noqa: ANN001
        """Execute workflow and push a completion event."""
        task = self._tasks[task_id]
        try:
            result = await self._engine.run_workflow(wf, trigger_type="background")
            task.status = result.status
            task.result = result
            task.finished_at = datetime.now(timezone.utc)

            # Build human-readable summary
            lines = [f"Workflow '{wf.name}' {result.status}."]
            for sr in result.step_results:
                icon = {"completed": "✓", "failed": "✗", "skipped": "⊘"}.get(
                    sr.status, "?"
                )
                detail = f" — {sr.error}" if sr.error else ""
                lines.append(f"  {icon} {sr.step_id}: {sr.status}{detail}")

            await self.events.put(
                BackgroundEvent(
                    task_id=task_id,
                    workflow_name=wf.name,
                    status=result.status,
                    summary="\n".join(lines),
                )
            )
            log.info("Background task %s finished: %s", task_id, result.status)

        except asyncio.CancelledError:
            task.status = "cancelled"
            task.finished_at = datetime.now(timezone.utc)
            log.info("Background task %s was cancelled", task_id)

        except Exception as exc:
            task.status = "failed"
            task.error = str(exc)
            task.finished_at = datetime.now(timezone.utc)

            await self.events.put(
                BackgroundEvent(
                    task_id=task_id,
                    workflow_name=wf.name,
                    status="failed",
                    summary=f"Workflow '{wf.name}' failed: {exc}",
                )
            )
            log.error("Background task %s failed: %s", task_id, exc)
