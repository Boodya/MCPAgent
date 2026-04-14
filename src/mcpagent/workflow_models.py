"""Workflow definition models and YAML loader with DAG validation."""

from __future__ import annotations

import logging
import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Variable interpolation  {{vars.X}} / {{steps.Y.result}}
# ---------------------------------------------------------------------------

_VAR_RE = re.compile(r"\{\{\s*([\w.'\"\[\]-]+)\s*\}\}")


def render_template(template: str, context: dict[str, Any]) -> str:
    """Replace ``{{vars.X}}`` and ``{{steps.Y.result}}`` placeholders."""

    def _resolve(match: re.Match) -> str:
        expr = match.group(1)
        parts = expr.split(".")
        obj: Any = context
        for part in parts:
            # dict-style: steps['gather'] → steps.gather handled via dict
            if isinstance(obj, dict):
                obj = obj.get(part)
            else:
                obj = getattr(obj, part, None)
            if obj is None:
                return match.group(0)  # leave unresolved
        return str(obj)

    return _VAR_RE.sub(_resolve, template)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class RetryConfig(BaseModel):
    count: int = 0
    delay: float = 5.0  # seconds between retries


class WorkflowStep(BaseModel):
    id: str
    agent: str = "default"
    prompt: str
    depends_on: list[str] = Field(default_factory=list)
    condition: str | None = None  # Python expression evaluated in safe sandbox
    timeout: int = 600  # seconds
    retry: RetryConfig = Field(default_factory=RetryConfig)
    on_failure: str = "stop"  # "stop" | "continue"


class WorkflowDefinition(BaseModel):
    name: str
    description: str = ""
    schedule: str | None = None     # cron expression (e.g. "0 9 * * *")
    interval: int | None = None     # interval in seconds (alternative to cron)
    enabled: bool = True
    vars: dict[str, Any] = Field(default_factory=dict)
    steps: list[WorkflowStep]

    @model_validator(mode="after")
    def validate_dag(self) -> "WorkflowDefinition":
        step_ids = {s.id for s in self.steps}
        for s in self.steps:
            for dep in s.depends_on:
                if dep not in step_ids:
                    raise ValueError(
                        f"Step '{s.id}' depends on '{dep}' which does not exist. "
                        f"Available: {step_ids}"
                    )
        # Check for cycles via topological sort
        if _has_cycle(self.steps):
            raise ValueError(f"Workflow '{self.name}' has a dependency cycle")
        return self


def _has_cycle(steps: list[WorkflowStep]) -> bool:
    """Kahn's algorithm — returns True if the step graph has a cycle."""
    in_degree: dict[str, int] = {s.id: 0 for s in steps}
    children: dict[str, list[str]] = defaultdict(list)
    for s in steps:
        for dep in s.depends_on:
            children[dep].append(s.id)
            in_degree[s.id] += 1

    queue = deque(sid for sid, deg in in_degree.items() if deg == 0)
    visited = 0
    while queue:
        node = queue.popleft()
        visited += 1
        for child in children[node]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)
    return visited != len(steps)


def topological_levels(steps: list[WorkflowStep]) -> list[list[WorkflowStep]]:
    """Return steps grouped by execution level (parallel within each level)."""
    step_map = {s.id: s for s in steps}
    in_degree: dict[str, int] = {s.id: 0 for s in steps}
    children: dict[str, list[str]] = defaultdict(list)
    for s in steps:
        for dep in s.depends_on:
            children[dep].append(s.id)
            in_degree[s.id] += 1

    levels: list[list[WorkflowStep]] = []
    queue = deque(sid for sid, deg in in_degree.items() if deg == 0)
    while queue:
        level: list[WorkflowStep] = []
        next_queue: deque[str] = deque()
        for sid in queue:
            level.append(step_map[sid])
        for s in level:
            for child in children[s.id]:
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    next_queue.append(child)
        levels.append(level)
        queue = next_queue
    return levels


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

class WorkflowLoader:
    """Scans a directory for workflow YAML files and parses them."""

    def __init__(self, workflows_dir: str | Path) -> None:
        self._dir = Path(workflows_dir)

    def load_all(self) -> list[WorkflowDefinition]:
        if not self._dir.is_dir():
            return []
        workflows: list[WorkflowDefinition] = []
        for p in sorted(self._dir.glob("*.yaml")):
            try:
                workflows.append(self.load_file(p))
            except Exception as exc:
                log.error("Failed to load workflow %s: %s", p.name, exc)
        for p in sorted(self._dir.glob("*.yml")):
            try:
                workflows.append(self.load_file(p))
            except Exception as exc:
                log.error("Failed to load workflow %s: %s", p.name, exc)
        return workflows

    def load_file(self, path: Path) -> WorkflowDefinition:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        if not isinstance(raw, dict):
            raise ValueError(f"{path.name}: expected a YAML mapping at top level")
        return WorkflowDefinition(**raw)

    def load_by_name(self, name: str) -> WorkflowDefinition | None:
        for wf in self.load_all():
            if wf.name == name:
                return wf
        return None
