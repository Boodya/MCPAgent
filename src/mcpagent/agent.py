"""ReAct agent loop — plan, execute tools, observe, iterate."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from mcpagent.agent_presets import AgentPreset, AgentPresetLoader
from mcpagent.config import AgentConfig
from mcpagent.context import ContextManager
from mcpagent.llm import LLMClient
from mcpagent.memory import MemoryManager
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
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.memory = memory
        self.config = config or AgentConfig()
        self.storage = storage
        self.preset_loader = preset_loader
        self.skill_loader = skill_loader
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

        # Inject user memory
        mem_summary = self.memory.load_user_memory_summary(max_lines=200)
        if mem_summary:
            system += f"\n\n<userMemory>\n{mem_summary}\n</userMemory>"

        self.messages = [{"role": "system", "content": system}]

    def switch_preset(self, name: str) -> str | None:
        """Switch to a named agent preset. Returns preset name on success, None on failure."""
        if not self.preset_loader:
            return None
        preset = self.preset_loader.switch(name)
        if preset:
            self._init_system_prompt()
            return preset.name
        return None

    def clear_history(self) -> None:
        """Reset conversation, keeping system prompt."""
        self._loaded_skills.clear()
        self._init_system_prompt()

    # ------------------------------------------------------------------
    # Main execution loop
    # ------------------------------------------------------------------

    async def run(self, user_message: str) -> AsyncIterator[AgentEvent]:
        """Process a user message through the ReAct loop, yielding events."""
        self.messages.append({"role": "user", "content": user_message})

        openai_tools = self.tools.to_openai_tools()

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
            stream = await self.llm.chat(self.messages, tools=openai_tools or None)

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

                # Execute all tool calls (parallel for independent calls)
                results = await self._execute_tool_calls(parsed_calls)

                for call, result in zip(parsed_calls, results):
                    yield AgentEvent(
                        type="tool_call",
                        tool_name=call.name,
                        tool_args=call.arguments,
                        tool_call_id=call.id,
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
