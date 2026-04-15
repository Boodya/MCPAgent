"""Scheduler daemon — runs workflows on cron/interval schedules."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

from mcpagent.config import load_app_config, resolve_dirs
from mcpagent.db import JobStore
from mcpagent.workflow_engine import WorkflowEngine
from mcpagent.workflow_models import WorkflowDefinition, WorkflowLoader

log = logging.getLogger(__name__)


class SchedulerService:
    """Built-in daemon that runs workflow schedules using asyncio + cron parsing."""

    def __init__(self, config_dir: str | Path | None = None) -> None:
        self._config_dir, self._base_dir = resolve_dirs(config_dir)

        self._config = load_app_config(self._config_dir)
        self._stop_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []  # type: ignore[type-arg]

    async def start(self) -> None:
        """Start the scheduler daemon. Blocks until stopped (Ctrl+C / SIGTERM)."""
        data_dir = Path(self._config.storage.data_dir)
        if not data_dir.is_absolute():
            data_dir = (self._base_dir / data_dir).resolve()
        else:
            data_dir = data_dir.resolve()
        db = JobStore(data_dir / "mcpagent.db")
        await db.init_db()

        workflows_dir = Path(self._config.workflows_dir)
        if not workflows_dir.is_absolute():
            candidate = self._base_dir / self._config.workflows_dir
            if candidate.is_dir():
                workflows_dir = candidate

        loader = WorkflowLoader(workflows_dir)
        workflows = [wf for wf in loader.load_all() if wf.enabled]

        if not workflows:
            print("No enabled workflows found. Nothing to schedule.")
            await db.close()
            return

        engine = WorkflowEngine(db)

        # Register signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop_event.set)
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                pass

        print(f"Scheduler started — {len(workflows)} workflow(s) active:")
        for wf in workflows:
            sched = wf.schedule or (f"every {wf.interval}s" if wf.interval else "manual")
            print(f"  • {wf.name} [{sched}]")

        # Launch a task per workflow
        for wf in workflows:
            task = asyncio.create_task(
                self._schedule_loop(wf, engine),
                name=f"wf-{wf.name}",
            )
            self._tasks.append(task)

        # Wait until stop signal
        await self._stop_event.wait()
        print("\nShutting down scheduler...")

        # Cancel all workflow tasks
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await db.close()
        print("Scheduler stopped.")

    async def _schedule_loop(
        self,
        wf: WorkflowDefinition,
        engine: WorkflowEngine,
    ) -> None:
        """Run a single workflow on its schedule (cron or interval)."""
        from mcpagent._cron import next_cron_delay

        try:
            while not self._stop_event.is_set():
                if wf.schedule:
                    delay = next_cron_delay(wf.schedule)
                elif wf.interval:
                    delay = float(wf.interval)
                else:
                    return  # no schedule — skip

                log.info("Workflow '%s' — next run in %.0fs", wf.name, delay)

                # Wait for delay or stop signal
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=delay
                    )
                    # If we get here, stop was requested
                    return
                except asyncio.TimeoutError:
                    pass  # delay elapsed — time to run

                log.info("Workflow '%s' — starting scheduled run", wf.name)
                try:
                    result = await engine.run_workflow(wf, trigger_type="scheduled")
                    print(f"  [{wf.name}] run #{result.run_id} — {result.status}")
                except Exception as exc:
                    log.error("Workflow '%s' failed: %s", wf.name, exc)
                    print(f"  [{wf.name}] failed: {exc}")

        except asyncio.CancelledError:
            pass

    async def show_status(self) -> None:
        """Show scheduled workflows and their next run times."""
        from mcpagent._cron import next_cron_delay

        workflows_dir = Path(self._config.workflows_dir)
        if not workflows_dir.is_absolute():
            candidate = self._base_dir / self._config.workflows_dir
            if candidate.is_dir():
                workflows_dir = candidate

        loader = WorkflowLoader(workflows_dir)
        workflows = loader.load_all()

        if not workflows:
            print("No workflows found.")
            return

        print(f"{'Name':<25} {'Schedule':<20} {'Enabled':<8} {'Next run in':<15}")
        print("-" * 70)
        for wf in workflows:
            sched = wf.schedule or (f"every {wf.interval}s" if wf.interval else "manual")
            if wf.enabled:
                if wf.schedule:
                    try:
                        delay = next_cron_delay(wf.schedule)
                        next_str = f"{delay:.0f}s"
                    except Exception:
                        next_str = "error"
                elif wf.interval:
                    next_str = f"{wf.interval}s"
                else:
                    next_str = "—"
            else:
                next_str = "disabled"
            print(f"{wf.name:<25} {sched:<20} {'yes' if wf.enabled else 'no':<8} {next_str:<15}")
