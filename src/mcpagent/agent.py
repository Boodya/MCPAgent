"""ReAct agent loop — plan, execute tools, observe, iterate."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from mcpagent.agent_presets import AgentPreset, AgentPresetLoader
from mcpagent.config import AgentConfig
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
                    triggers = f" (triggers: {', '.join(s.triggers)})" if s.triggers else ""
                    catalog_lines.append(f"- {s.name}{desc}{triggers}")
                catalog = "\n".join(catalog_lines)
                system += (
                    "\n\n<availableSkills>\n"
                    "The following skills are available. When a user's request matches a skill, "
                    "the skill instructions will be automatically injected into the conversation. "
                    "Follow skill instructions when they are provided.\n"
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
        self._init_system_prompt()

    # ------------------------------------------------------------------
    # Main execution loop
    # ------------------------------------------------------------------

    async def run(self, user_message: str) -> AsyncIterator[AgentEvent]:
        """Process a user message through the ReAct loop, yielding events."""
        self.messages.append({"role": "user", "content": user_message})

        # --- Skill matching: inject matched skill instructions ---
        activated_skills = self._match_and_inject_skills(user_message)
        for skill in activated_skills:
            yield AgentEvent(type="skill_activated", content=skill.name, tool_name=skill.name)

        openai_tools = self.tools.to_openai_tools()

        for iteration in range(self.config.max_iterations):
            log.debug("Agent iteration %d", iteration + 1)

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

                    # Add tool result to messages
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": result,
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
    # Skill orchestration
    # ------------------------------------------------------------------

    def _match_and_inject_skills(self, user_message: str) -> list[Skill]:
        """Match user message against available skills, inject content as system context."""
        if not self.skill_loader:
            return []

        matched = self.skill_loader.match(user_message)
        if not matched:
            return []

        # Build a single system message with all matched skill instructions
        parts: list[str] = []
        for skill in matched:
            content = self.skill_loader.load_content(skill)
            parts.append(f"<skill name=\"{skill.name}\">\n{content}\n</skill>")

        skill_msg = (
            "The following skill instructions have been activated based on the user's request. "
            "Follow these instructions carefully:\n\n" + "\n\n".join(parts)
        )

        # Insert as a system message right before the user message (last in list)
        # so the LLM sees: system prompt → ... → skill context → user message
        self.messages.insert(-1, {"role": "system", "content": skill_msg})

        log.info("Activated skills: %s", [s.name for s in matched])
        return matched
