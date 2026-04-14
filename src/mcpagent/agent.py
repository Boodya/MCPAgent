"""ReAct agent loop — plan, execute tools, observe, iterate."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from pydantic import BaseModel, Field as PydanticField

from mcpagent.agent_presets import AgentPreset, AgentPresetLoader
from mcpagent.background import BackgroundManager
from mcpagent.config import AgentConfig
from mcpagent.context import ContextManager
from mcpagent.llm import LLMClient
from mcpagent.memory import MemoryManager
from mcpagent.mcp_manager import MCPManager
from mcpagent.ops_log import OpsLog
from mcpagent.skills import Skill, SkillLoader
from mcpagent.storage import StorageManager
from mcpagent.tools import ToolRegistry

log = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = """\
You are a powerful AI assistant with access to tools.
You help the user by breaking down tasks, calling tools as needed, and synthesizing results.
Follow the user's requirements carefully.

Available capabilities:
- Read/write files on disk
- Search through code and text
- Run shell commands
- Manage persistent memory (markdown files organized by scope: user, session, repo)
- Access external tools via MCP servers

When working on tasks:
1. Plan your approach before acting
2. Use tools to gather information and make changes
3. Report results clearly and concisely

File operations:
- Check `<platformPaths>` in your context for the correct directories.
- Always write generated artifacts (agents, skills, workflows, reports) to the configured paths — never to the project root.

For memory management:
- Use memory_view to check existing notes before creating new ones
- Use memory_create to save important findings and decisions
- Organize by topic in separate files (e.g. memories/user/patterns.md)
- Keep notes concise — use bullet points
"""


@dataclass
class ToolCallRequest:
    """A pending tool call from the LLM."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class AgentEvent:
    """Events yielded by the agent during execution."""

    type: str  # "text", "tool_call", "tool_result", "error", "done"
    content: str = ""
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    tool_call_id: str = ""


class AgentResult(BaseModel):
    """Collected result of a headless agent run."""

    text: str = ""
    tool_calls: list[dict[str, Any]] = PydanticField(default_factory=list)
    error: str | None = None


class Agent:
    """ReAct agent: streams LLM responses, dispatches tool calls, iterates."""

    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRegistry,
        memory: MemoryManager,
        config: AgentConfig | None = None,
        storage: StorageManager | None = None,
        preset_loader: AgentPresetLoader | None = None,
        skill_loader: SkillLoader | None = None,
        mcp_manager: MCPManager | None = None,
        background: BackgroundManager | None = None,
        ops: OpsLog | None = None,
        platform_paths: dict[str, str] | None = None,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.memory = memory
        self.config = config or AgentConfig()
        self.storage = storage
        self.preset_loader = preset_loader
        self.skill_loader = skill_loader
        self.mcp_manager = mcp_manager
        self.background = background
        self.ops = ops or OpsLog(None)
        self.platform_paths = platform_paths or {}
        self.messages: list[dict[str, Any]] = []

        # Context window management
        self.ctx = ContextManager(
            context_window=self.config.context_window,
            summarize_threshold=self.config.summarize_threshold,
            max_tool_result_tokens=self.config.max_tool_result_tokens,
            summary_max_tokens=self.config.summary_max_tokens,
        )

        # Track which skills are loaded in this conversation
        self._loaded_skills: set[str] = set()

        # Register skill tools into the tool registry
        self._register_skill_tools()

        # Register subagent tool if any agent defines subagents
        self._register_subagent_tool()

        # Register background workflow tools
        self._register_workflow_tools()

        self._init_system_prompt()

    @property
    def active_preset(self) -> AgentPreset | None:
        return self.preset_loader.active if self.preset_loader else None

    @property
    def active_agent_name(self) -> str:
        if self.preset_loader:
            return self.preset_loader.active.name
        return "default"

    def _init_system_prompt(self) -> None:
        # Prefer preset system prompt, then config, then built-in default
        preset = self.active_preset
        if preset and preset.system_prompt:
            system = preset.system_prompt
        else:
            system = self.config.system_prompt or DEFAULT_SYSTEM_PROMPT

        # Inject skills catalog so the LLM knows what's available
        if self.skill_loader:
            skills = self.skill_loader.get_all()
            if skills:
                catalog_lines = []
                for s in skills:
                    desc = f" — {s.description}" if s.description else ""
                    catalog_lines.append(f"- {s.name}{desc}")
                catalog = "\n".join(catalog_lines)
                system += (
                    "\n\n<availableSkills>\n"
                    "You have specialized skill modules that contain expert instructions "
                    "for specific tasks. BEFORE starting work on a task, review this catalog "
                    "and call `load_skill` for any relevant skill. "
                    "This loads detailed instructions that improve your output quality.\n"
                    "You can load multiple skills if needed. Already-loaded skills are skipped automatically.\n"
                    "\nAvailable skills:\n"
                    f"{catalog}\n"
                    "</availableSkills>"
                )

        # Inject available subagents so the LLM knows valid call_agent targets
        if preset and preset.subagents and self.preset_loader:
            agent_lines = []
            for agent_name in preset.subagents:
                target = self.preset_loader.presets.get(agent_name)
                if target:
                    desc = f" — {target.description}" if target.description else ""
                    agent_lines.append(f"- {agent_name}{desc}")
                else:
                    agent_lines.append(f"- {agent_name} (not found)")
            if agent_lines:
                catalog = "\n".join(agent_lines)
                system += (
                    "\n\n<availableSubagents>\n"
                    "You can delegate tasks to these sub-agents via `call_agent` tool.\n"
                    "Only the agents listed below are available. Do NOT invent agent names.\n"
                    f"\n{catalog}\n"
                    "</availableSubagents>"
                )

        # Inject available workflows for background execution
        if self.background:
            wf_names = self.background.get_workflow_names()
            if wf_names:
                wf_list = "\n".join(f"- {n}" for n in wf_names)
                system += (
                    "\n\n<backgroundWorkflows>\n"
                    "You can run workflows in the background using the `workflow_run` tool. "
                    "This starts a workflow asynchronously and returns immediately — the user "
                    "can continue chatting while it runs. You will be notified when it completes.\n"
                    "Use `workflow_status` to check on running tasks, "
                    "and `workflow_list` to see available workflows.\n"
                    f"\nAvailable workflows:\n{wf_list}\n"
                    "</backgroundWorkflows>"
                )

        # Inject platform paths so the agent knows where to read/write artifacts
        if self.platform_paths:
            path_lines = "\n".join(f"- {k}: {v}" for k, v in self.platform_paths.items())
            system += (
                "\n\n<platformPaths>\n"
                "IMPORTANT: Always use these configured paths for file operations. "
                "Never create files in the project root — use the paths below.\n"
                f"\n{path_lines}\n"
                "</platformPaths>"
            )

        # Inject user memory
        mem_summary = self.memory.load_user_memory_summary(max_lines=200)
        if mem_summary:
            system += f"\n\n<userMemory>\n{mem_summary}\n</userMemory>"

        self.messages = [{"role": "system", "content": system}]

    async def switch_preset(self, name: str) -> str | None:
        """Switch to a named agent preset. Manages MCP server lifecycle.

        Returns preset name on success, None on failure.
        """
        if not self.preset_loader:
            return None
        preset = self.preset_loader.switch(name)
        if not preset:
            return None

        # Bring MCP servers in sync with the new agent's requirements
        if self.mcp_manager:
            started, stopped = await self.mcp_manager.ensure_servers(preset.mcp_servers)
            if started:
                log.info("Started MCP servers for agent '%s': %s", name, started)
            if stopped:
                log.info("Stopped MCP servers for agent '%s': %s", name, stopped)

        self._loaded_skills.clear()
        self._init_system_prompt()
        return preset.name

    def clear_history(self) -> None:
        """Reset conversation, keeping system prompt."""
        self._loaded_skills.clear()
        self._init_system_prompt()

    async def run_to_completion(self, message: str) -> AgentResult:
        """Run the agent headlessly and return the collected result."""
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        error: str | None = None

        async for event in self.run(message):
            if event.type == "text":
                text_parts.append(event.content)
            elif event.type == "tool_call":
                tool_calls.append({
                    "name": event.tool_name,
                    "args": event.tool_args,
                    "id": event.tool_call_id,
                })
            elif event.type == "error":
                error = event.content

        return AgentResult(
            text="".join(text_parts),
            tool_calls=tool_calls,
            error=error,
        )

    # ------------------------------------------------------------------
    # Main execution loop
    # ------------------------------------------------------------------

    async def run(self, user_message: str) -> AsyncIterator[AgentEvent]:
        """Process a user message through the ReAct loop, yielding events."""
        self.messages.append({"role": "user", "content": user_message})

        openai_tools = self.tools.to_openai_tools(
            allowed=self.active_preset.tools if self.active_preset else None,
        )

        for iteration in range(self.config.max_iterations):
            log.debug("Agent iteration %d", iteration + 1)

            # --- Context window management: summarize if needed ---
            if self.ctx.needs_summarization(self.messages):
                yield AgentEvent(type="context_summarizing", content="Summarizing conversation history...")
                self.messages = await self.ctx.maybe_summarize(self.messages, self.llm)
                yield AgentEvent(
                    type="context_summarized",
                    content=f"Context compressed (summarization #{self.ctx.summarization_count})",
                )

            # --- Call LLM (streaming) ---
            stream = await self.llm.chat(
                self.messages,
                tools=openai_tools or None,
                agent_name=self.active_agent_name,
            )

            # Accumulate the full response from stream
            collected_text = ""
            tool_calls_by_index: dict[int, dict[str, Any]] = {}

            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if not delta:
                    continue

                # Text content
                if delta.content:
                    collected_text += delta.content
                    yield AgentEvent(type="text", content=delta.content)

                # Tool calls (streamed incrementally)
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_by_index:
                            tool_calls_by_index[idx] = {
                                "id": tc.id or "",
                                "name": "",
                                "arguments": "",
                            }
                        entry = tool_calls_by_index[idx]
                        if tc.id:
                            entry["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                entry["name"] = tc.function.name
                            if tc.function.arguments:
                                entry["arguments"] += tc.function.arguments

            # --- Process accumulated response ---

            # If we got tool calls, execute them
            if tool_calls_by_index:
                # Build assistant message with tool_calls
                assistant_msg: dict[str, Any] = {"role": "assistant", "content": collected_text or None}
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": tc["arguments"]},
                    }
                    for tc in tool_calls_by_index.values()
                ]
                self.messages.append(assistant_msg)

                # Parse and dispatch tool calls
                parsed_calls: list[ToolCallRequest] = []
                for tc in tool_calls_by_index.values():
                    try:
                        args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                    except json.JSONDecodeError:
                        args = {}
                    parsed_calls.append(ToolCallRequest(id=tc["id"], name=tc["name"], arguments=args))

                # Execute tool calls: yield tool_call BEFORE execution, tool_result AFTER
                for call in parsed_calls:
                    yield AgentEvent(
                        type="tool_call",
                        tool_name=call.name,
                        tool_args=call.arguments,
                        tool_call_id=call.id,
                    )
                    self.ops.tool_call(
                        agent=self.active_agent_name,
                        tool=call.name,
                        args=call.arguments,
                    )

                results = await self._execute_tool_calls(parsed_calls)

                for call, result in zip(parsed_calls, results):
                    is_error = result.startswith('{"error"')
                    self.ops.tool_result(
                        agent=self.active_agent_name,
                        tool=call.name,
                        result_length=len(result),
                        error=result if is_error else None,
                    )

                    yield AgentEvent(
                        type="tool_result",
                        content=result,
                        tool_name=call.name,
                        tool_call_id=call.id,
                    )

                    # Log tool execution
                    if self.storage:
                        self.storage.log_event(
                            "tool_call",
                            tool=call.name,
                            args=call.arguments,
                            result_length=len(result),
                        )

                    # Add tool result to messages (truncated if too large)
                    truncated_result = self.ctx.truncate_tool_result(result)
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": truncated_result,
                    })

                # Continue to next iteration (LLM will see tool results)
                continue

            # No tool calls — we have the final text answer
            if collected_text:
                self.messages.append({"role": "assistant", "content": collected_text})

            yield AgentEvent(type="done")
            return

        # Max iterations reached
        yield AgentEvent(type="error", content="Max iterations reached. Stopping.")
        yield AgentEvent(type="done")

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def _execute_tool_calls(self, calls: list[ToolCallRequest]) -> list[str]:
        """Execute tool calls concurrently."""
        tasks = [self.tools.dispatch(c.name, c.arguments) for c in calls]
        return await asyncio.gather(*tasks)

    # ------------------------------------------------------------------
    # Skill tools
    # ------------------------------------------------------------------

    def _register_skill_tools(self) -> None:
        """Register load_skill tool so the LLM can load skills on-demand."""
        if not self.skill_loader or not self.skill_loader.get_all():
            return

        from mcpagent.tools import _schema

        async def _handle_load_skill(args: dict[str, Any]) -> str:
            return self._load_skill(args.get("name", ""))

        self.tools.register(
            "load_skill",
            _handle_load_skill,
            (
                "Load a skill module by name. Call this BEFORE starting a task "
                "when a relevant skill is available. Returns the skill instructions "
                "to follow. Use /skills or the <availableSkills> catalog to see what's available."
            ),
            _schema(
                {"name": {"type": "string", "description": "Exact skill name from the catalog."}},
                required=["name"],
            ),
        )

    def _load_skill(self, name: str) -> str:
        """Load a skill by name and return its content."""
        if not self.skill_loader:
            return json.dumps({"error": "Skills not configured."})

        # Find skill by name
        skill = None
        for s in self.skill_loader.get_all():
            if s.name == name:
                skill = s
                break

        if not skill:
            available = [s.name for s in self.skill_loader.get_all()]
            return json.dumps({"error": f"Skill '{name}' not found. Available: {available}"})

        # Track loaded skill
        was_new = name not in self._loaded_skills
        self._loaded_skills.add(name)

        if not was_new:
            return json.dumps({"status": "already_loaded", "skill": name,
                               "message": f"Skill '{name}' is already loaded. Proceed with the task."})

        content = self.skill_loader.load_content(skill)
        log.info("Loaded skill: %s", name)

        return (
            f"Skill '{name}' loaded. Follow these instructions for the current task:\n\n"
            f"<skill name=\"{name}\">\n{content}\n</skill>"
        )

    # ------------------------------------------------------------------
    # Subagent tool
    # ------------------------------------------------------------------

    def _register_subagent_tool(self) -> None:
        """Register call_agent tool so the LLM can invoke other agents as sub-agents."""
        if not self.preset_loader:
            return

        # Check if any agent defines subagents
        has_subagents = any(p.subagents for p in self.preset_loader.get_all())
        if not has_subagents:
            return

        from mcpagent.tools import _schema

        async def _handle_call_agent(args: dict[str, Any]) -> str:
            return await self._call_agent(args.get("name", ""), args.get("message", ""))

        self.tools.register(
            "call_agent",
            _handle_call_agent,
            (
                "Invoke another agent as a sub-agent. The sub-agent runs a full "
                "conversation with its own system prompt and tools, then returns "
                "its final response. Use this to delegate specialized tasks. "
                "IMPORTANT: only use agent names listed in <availableSubagents> in your system prompt."
            ),
            _schema(
                {
                    "name": {"type": "string", "description": "Name of the agent to invoke. Must be one of the agents listed in <availableSubagents>."},
                    "message": {"type": "string", "description": "The task or question to send to the sub-agent."},
                },
                required=["name", "message"],
            ),
        )

    async def _call_agent(self, name: str, message: str) -> str:
        """Run a sub-agent and return its final text response."""
        if not self.preset_loader:
            return json.dumps({"error": "Agent presets not configured."})

        # Check if current agent is allowed to call this sub-agent
        current = self.active_preset
        if current and name not in (current.subagents or []):
            return json.dumps({
                "error": f"Agent '{self.active_agent_name}' cannot call sub-agent '{name}'. "
                         f"Allowed subagents: {current.subagents}"
            })

        preset = self.preset_loader.presets.get(name)
        if not preset:
            available = self.preset_loader.get_names()
            return json.dumps({"error": f"Agent '{name}' not found. Available: {available}"})

        log.info("Calling sub-agent '%s' with message: %s", name, message[:100])

        # Build sub-agent system prompt (same logic as _init_system_prompt but for the target preset)
        if preset.system_prompt:
            system = preset.system_prompt
        else:
            system = DEFAULT_SYSTEM_PROMPT

        # Inject skills catalog for the sub-agent too
        if self.skill_loader:
            skills = self.skill_loader.get_all()
            if skills:
                catalog_lines = []
                for s in skills:
                    desc = f" — {s.description}" if s.description else ""
                    catalog_lines.append(f"- {s.name}{desc}")
                catalog = "\n".join(catalog_lines)
                system += (
                    "\n\n<availableSkills>\n"
                    "You have specialized skill modules. Call `load_skill` for relevant skills.\n"
                    f"\nAvailable skills:\n{catalog}\n"
                    "</availableSkills>"
                )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": message},
        ]

        # Sub-agent uses the same tool registry and MCP connections
        # but respects its own tool filter
        openai_tools = self.tools.to_openai_tools(allowed=preset.tools)

        # Run a limited ReAct loop for the sub-agent
        max_iter = min(self.config.max_iterations, 15)  # cap sub-agent iterations
        final_text = ""

        for iteration in range(max_iter):
            log.debug("Sub-agent '%s' iteration %d", name, iteration + 1)

            stream = await self.llm.chat(
                messages,
                tools=openai_tools or None,
                agent_name=f"sub:{name}",
            )

            collected_text = ""
            tool_calls_by_index: dict[int, dict[str, Any]] = {}

            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if not delta:
                    continue
                if delta.content:
                    collected_text += delta.content
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_by_index:
                            tool_calls_by_index[idx] = {"id": tc.id or "", "name": "", "arguments": ""}
                        entry = tool_calls_by_index[idx]
                        if tc.id:
                            entry["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                entry["name"] = tc.function.name
                            if tc.function.arguments:
                                entry["arguments"] += tc.function.arguments

            if tool_calls_by_index:
                assistant_msg: dict[str, Any] = {"role": "assistant", "content": collected_text or None}
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": tc["arguments"]},
                    }
                    for tc in tool_calls_by_index.values()
                ]
                messages.append(assistant_msg)

                # Execute tool calls
                for tc in tool_calls_by_index.values():
                    try:
                        args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                    except json.JSONDecodeError:
                        args = {}

                    result = await self.tools.dispatch(tc["name"], args)
                    truncated = self.ctx.truncate_tool_result(result)
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": truncated})

                continue

            # No tool calls — final answer
            final_text = collected_text
            break

        if not final_text:
            final_text = "(Sub-agent did not produce a final response)"

        log.info("Sub-agent '%s' completed", name)
        return f"[Sub-agent: {name}]\n{final_text}"

    # ------------------------------------------------------------------
    # Background workflow tools
    # ------------------------------------------------------------------

    def _register_workflow_tools(self) -> None:
        """Register workflow tools so the LLM can run workflows in the background."""
        if not self.background:
            return

        from mcpagent.tools import _schema

        # --- workflow_run ---

        async def _handle_workflow_run(args: dict[str, Any]) -> str:
            name = args.get("name", "")
            try:
                task_id = self.background.submit(name)  # type: ignore[union-attr]
                return json.dumps({
                    "status": "submitted",
                    "task_id": task_id,
                    "workflow": name,
                    "message": f"Workflow '{name}' started in background as {task_id}. "
                               f"You will be notified when it completes.",
                })
            except ValueError as exc:
                return json.dumps({"error": str(exc)})

        self.tools.register(
            "workflow_run",
            _handle_workflow_run,
            (
                "Start a workflow in the background. The workflow runs asynchronously "
                "while the user continues chatting. Returns a task ID immediately. "
                "You will receive a notification when the workflow completes."
            ),
            _schema(
                {"name": {"type": "string", "description": "Name of the workflow to run."}},
                required=["name"],
            ),
        )

        # --- workflow_list ---

        async def _handle_workflow_list(args: dict[str, Any]) -> str:
            names = self.background.get_workflow_names()  # type: ignore[union-attr]
            return json.dumps({"workflows": names})

        self.tools.register(
            "workflow_list",
            _handle_workflow_list,
            "List all available workflow definitions that can be run.",
            _schema({}),
        )

        # --- workflow_status ---

        async def _handle_workflow_status(args: dict[str, Any]) -> str:
            task_id = args.get("task_id")
            tasks = self.background.get_tasks(task_id)  # type: ignore[union-attr]
            if not tasks:
                return json.dumps({"message": "No background tasks found."})
            result = []
            for t in tasks:
                entry = {
                    "task_id": t.id,
                    "workflow": t.workflow_name,
                    "status": t.status,
                    "started_at": t.started_at.isoformat(),
                }
                if t.finished_at:
                    entry["finished_at"] = t.finished_at.isoformat()
                if t.error:
                    entry["error"] = t.error
                result.append(entry)
            return json.dumps({"tasks": result})

        self.tools.register(
            "workflow_status",
            _handle_workflow_status,
            (
                "Check the status of background workflow tasks. "
                "Call without arguments to see all tasks, or pass a task_id to check a specific one."
            ),
            _schema(
                {"task_id": {"type": "string", "description": "Optional task ID to check (e.g. 'bg-1')."}},
            ),
        )
