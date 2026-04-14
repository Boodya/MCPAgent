"""Workflow engine — DAG resolution, step execution, variable interpolation."""

from __future__ import annotations

import ast
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from mcpagent.db import JobStore
from mcpagent.headless import create_agent
from mcpagent.ops_log import OpsLog
from mcpagent.workflow_models import (
    WorkflowDefinition,
    WorkflowStep,
    render_template,
    topological_levels,
)

log = logging.getLogger(__name__)


@dataclass
class StepResult:
    step_id: str
    status: str  # "completed" | "failed" | "skipped"
    result: str = ""
    error: str | None = None


@dataclass
class RunResult:
    run_id: int
    status: str  # "completed" | "failed"
    error: str | None = None
    step_results: list[StepResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Safe condition evaluation
# ---------------------------------------------------------------------------

_SAFE_BUILTINS = {"len": len, "str": str, "int": int, "float": float, "bool": bool, "True": True, "False": False}


def _eval_condition(expr: str, steps_ctx: dict[str, Any]) -> bool:
    """Evaluate a condition expression in a restricted sandbox.

    Only allows access to ``steps`` dict (with ``.result`` / ``.status``
    attributes) and a small set of builtins.
    """
    try:
        # Parse to AST to reject dangerous constructs
        tree = ast.parse(expr, mode="eval")
        for node in ast.walk(tree):
            # Block attribute access to anything except known safe attrs
            if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
                raise ValueError(f"Access to private attribute '{node.attr}' is not allowed")
            if isinstance(node, ast.Call):
                # Only allow builtins from whitelist
                if isinstance(node.func, ast.Name) and node.func.id not in _SAFE_BUILTINS:
                    raise ValueError(f"Function '{node.func.id}' is not allowed")

        code = compile(tree, "<condition>", "eval")
        result = eval(code, {"__builtins__": _SAFE_BUILTINS, "steps": steps_ctx})
        return bool(result)
    except Exception as exc:
        log.warning("Condition eval failed for '%s': %s", expr, exc)
        return False


# ---------------------------------------------------------------------------
# Workflow engine
# ---------------------------------------------------------------------------

class WorkflowEngine:
    """Execute a workflow DAG: topological sort → parallel execution per level."""

    def __init__(self, db: JobStore, *, ops: OpsLog | None = None) -> None:
        self.db = db
        self.ops = ops or OpsLog(None)

    async def run_workflow(
        self,
        workflow: WorkflowDefinition,
        trigger_type: str = "manual",
    ) -> RunResult:
        run_id = await self.db.create_run(workflow.name, trigger_type)
        log.info("Starting workflow '%s' (run #%d)", workflow.name, run_id)
        self.ops.workflow_start(workflow=workflow.name, run_id=run_id)

        # Build context dicts for template rendering
        vars_ctx = dict(workflow.vars)
        step_results: dict[str, StepResult] = {}
        # For condition eval: steps_ctx['step-id'] has .result and .status
        steps_ctx: dict[str, _StepCtx] = {}

        failed_steps: set[str] = set()
        all_step_results: list[StepResult] = []

        levels = topological_levels(workflow.steps)

        try:
            for level_idx, level in enumerate(levels):
                log.info("Workflow '%s' run #%d — level %d: %s",
                         workflow.name, run_id, level_idx,
                         [s.id for s in level])

                tasks = []
                for step in level:
                    # Skip if any dependency failed (and on_failure != continue)
                    skip = False
                    for dep in step.depends_on:
                        if dep in failed_steps:
                            skip = True
                            break

                    if skip:
                        sr = StepResult(step_id=step.id, status="skipped")
                        step_results[step.id] = sr
                        steps_ctx[step.id] = _StepCtx(result="", status="skipped")
                        all_step_results.append(sr)
                        # Record in DB
                        sr_id = await self.db.create_step_run(
                            run_id, step.id, step.agent, prompt_rendered="")
                        await self.db.update_step_run(sr_id, status="skipped")
                        continue

                    tasks.append(self._run_step(
                        step, run_id, workflow.name, vars_ctx, step_results, steps_ctx,
                    ))

                # Execute steps in this level concurrently
                if tasks:
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for r in results:
                        if isinstance(r, Exception):
                            log.error("Step execution error: %s", r)
                            continue
                        sr = r
                        all_step_results.append(sr)
                        if sr.status == "failed" and sr.step_id not in failed_steps:
                            # Check if step allows continue
                            step_def = next(
                                (s for s in level if s.id == sr.step_id), None)
                            if step_def and step_def.on_failure != "continue":
                                failed_steps.add(sr.step_id)

            # Determine overall status
            has_failures = any(sr.status == "failed" for sr in all_step_results)
            status = "failed" if has_failures else "completed"
            await self.db.update_run(run_id, status=status)
            log.info("Workflow '%s' run #%d — %s", workflow.name, run_id, status)
            self.ops.workflow_end(workflow=workflow.name, run_id=run_id, status=status)

            return RunResult(
                run_id=run_id,
                status=status,
                step_results=all_step_results,
            )

        except Exception as exc:
            await self.db.update_run(run_id, status="failed", error=str(exc))
            log.error("Workflow '%s' run #%d failed: %s", workflow.name, run_id, exc)
            self.ops.workflow_end(workflow=workflow.name, run_id=run_id, status="failed", error=str(exc))
            return RunResult(
                run_id=run_id,
                status="failed",
                error=str(exc),
                step_results=all_step_results,
            )

    async def _run_step(
        self,
        step: WorkflowStep,
        run_id: int,
        workflow_name: str,
        vars_ctx: dict[str, Any],
        step_results: dict[str, StepResult],
        steps_ctx: dict[str, "_StepCtx"],
    ) -> StepResult:
        """Execute a single workflow step."""

        # Evaluate condition
        if step.condition:
            if not _eval_condition(step.condition, steps_ctx):
                log.info("Step '%s' skipped (condition false)", step.id)
                sr = StepResult(step_id=step.id, status="skipped")
                step_results[step.id] = sr
                steps_ctx[step.id] = _StepCtx(result="", status="skipped")
                sr_id = await self.db.create_step_run(
                    run_id, step.id, step.agent, prompt_rendered="")
                await self.db.update_step_run(sr_id, status="skipped")
                return sr

        # Render prompt template
        render_ctx = {
            "vars": vars_ctx,
            "steps": {sid: {"result": sr.result, "status": sr.status}
                      for sid, sr in step_results.items()},
        }
        rendered_prompt = render_template(step.prompt, render_ctx)

        sr_id = await self.db.create_step_run(
            run_id, step.id, step.agent, prompt_rendered=rendered_prompt)
        self.ops.workflow_step_start(
            workflow=workflow_name, run_id=run_id, step_id=step.id, agent=step.agent)

        # Retry loop
        last_error: str | None = None
        for attempt in range(1 + step.retry.count):
            if attempt > 0:
                log.info("Step '%s' retry %d/%d", step.id, attempt, step.retry.count)
                await asyncio.sleep(step.retry.delay)

            try:
                agent, cleanup = await create_agent(agent_name=step.agent, ops=self.ops)
                try:
                    result = await asyncio.wait_for(
                        agent.run_to_completion(rendered_prompt),
                        timeout=step.timeout,
                    )
                finally:
                    try:
                        await cleanup()
                    except Exception:
                        pass

                if result.error:
                    last_error = result.error
                    continue

                # Success
                sr = StepResult(
                    step_id=step.id, status="completed", result=result.text)
                step_results[step.id] = sr
                steps_ctx[step.id] = _StepCtx(result=result.text, status="completed")
                await self.db.update_step_run(
                    sr_id, status="completed", result_text=result.text)
                self.ops.workflow_step_end(
                    workflow=workflow_name, run_id=run_id, step_id=step.id,
                    agent=step.agent, status="completed")
                log.info("Step '%s' completed", step.id)
                return sr

            except asyncio.TimeoutError:
                last_error = f"Step '{step.id}' timed out after {step.timeout}s"
                log.warning(last_error)
            except Exception as exc:
                last_error = str(exc)
                log.error("Step '%s' error: %s", step.id, exc)

        # All retries exhausted
        sr = StepResult(step_id=step.id, status="failed", error=last_error)
        step_results[step.id] = sr
        steps_ctx[step.id] = _StepCtx(result="", status="failed")
        await self.db.update_step_run(sr_id, status="failed", error=last_error)
        self.ops.workflow_step_end(
            workflow=workflow_name, run_id=run_id, step_id=step.id,
            agent=step.agent, status="failed", error=last_error)
        log.error("Step '%s' failed: %s", step.id, last_error)
        return sr


class _StepCtx:
    """Minimal object for condition evaluation — exposes .result and .status."""

    __slots__ = ("result", "status")

    def __init__(self, result: str, status: str) -> None:
        self.result = result
        self.status = status

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)
