"""Rich CLI interface — streaming output, tool call display, slash commands."""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from rich.theme import Theme

if TYPE_CHECKING:
    from mcpagent.agent import Agent, AgentEvent
    from mcpagent.mcp_manager import MCPManager
    from mcpagent.skills import SkillLoader
    from mcpagent.storage import StorageManager
    from mcpagent.tools import ToolRegistry

THEME = Theme({
    "tool.name": "bold cyan",
    "tool.result": "dim",
    "info": "bold blue",
    "error": "bold red",
    "user.prompt": "bold green",
    "agent.name": "bold magenta",
})


class CLI:
    """Interactive Rich-based CLI for the agent."""

    def __init__(
        self,
        agent: Agent,
        tools: ToolRegistry,
        mcp: MCPManager | None = None,
        storage: StorageManager | None = None,
        skill_loader: SkillLoader | None = None,
    ) -> None:
        self.agent = agent
        self.tools = tools
        self.mcp = mcp
        self.storage = storage
        self.skill_loader = skill_loader
        self.console = Console(theme=THEME)

    # ------------------------------------------------------------------
    # Main REPL
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start the interactive REPL."""
        self.console.print(
            Panel(
                "[bold]MCPAgent[/bold] — Universal AI Agent with MCP\n"
                "Type your message or use /help for commands. Ctrl+C to exit.",
                style="info",
            )
        )

        self._print_status()

        while True:
            try:
                user_input = await asyncio.to_thread(self._get_input)
            except (EOFError, KeyboardInterrupt):
                self.console.print("\n[info]Goodbye![/info]")
                break

            if not user_input.strip():
                continue

            # Slash commands
            if user_input.startswith("/"):
                should_continue = self._handle_command(user_input.strip())
                if not should_continue:
                    break
                continue

            # Run agent
            await self._run_agent(user_input)

    def _get_input(self) -> str:
        agent_name = self.agent.active_agent_name
        try:
            return input(f"\n[{agent_name}] > ")
        except EOFError:
            raise

    # ------------------------------------------------------------------
    # Agent execution with streaming display
    # ------------------------------------------------------------------

    async def _run_agent(self, user_input: str) -> None:
        self.console.print()
        text_buffer = ""
        skills_shown = False

        try:
            async for event in self.agent.run(user_input):
                if event.type == "text":
                    text_buffer += event.content
                    # Print character-by-character for streaming feel
                    print(event.content, end="", flush=True)

                elif event.type == "tool_call":
                    # If we had text buffered, finish the line
                    if text_buffer:
                        print()
                        text_buffer = ""
                    args_str = _truncate(str(event.tool_args), 200)
                    self.console.print(
                        f"  [tool.name]⚡ {event.tool_name}[/tool.name]({args_str})"
                    )

                elif event.type == "skill_activated":
                    if not skills_shown:
                        self.console.print("[bold yellow]📚 Skills activated:[/bold yellow]")
                        skills_shown = True
                    self.console.print(f"  [bold yellow]→[/bold yellow] [bold]{event.content}[/bold]")

                elif event.type == "tool_result":
                    result_preview = _truncate(event.content, 300)
                    self.console.print(f"  [tool.result]→ {result_preview}[/tool.result]")

                elif event.type == "error":
                    self.console.print(f"[error]Error: {event.content}[/error]")

                elif event.type == "done":
                    if text_buffer:
                        print()  # Final newline after streaming
                    break

        except KeyboardInterrupt:
            self.console.print("\n[info]Interrupted.[/info]")
        except Exception as exc:
            self.console.print(f"[error]Agent error: {exc}[/error]")

        # Save chat history + flush logs after every exchange
        if self.storage:
            self.storage.save_chat(self.agent.messages)
            self.storage.flush_logs()

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------

    def _handle_command(self, cmd: str) -> bool:
        """Handle a slash command. Returns False to exit the REPL."""
        parts = cmd.split(maxsplit=1)
        command = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if command in ("/exit", "/quit"):
            self.console.print("[info]Goodbye![/info]")
            return False

        if command == "/help":
            self._cmd_help()
            return True

        if command == "/clear":
            self.agent.clear_history()
            self.console.print("[info]Conversation cleared.[/info]")
            return True

        if command == "/tools":
            self._cmd_tools()
            return True

        if command == "/servers":
            self._print_status()
            return True

        if command == "/memory":
            self._cmd_memory()
            return True

        if command == "/agents":
            self._cmd_agents()
            return True

        if command == "/agent":
            self._cmd_agent_switch(arg)
            return True

        if command == "/skills":
            self._cmd_skills()
            return True

        self.console.print(f"[error]Unknown command: {command}. Type /help[/error]")
        return True

    # ------------------------------------------------------------------
    # Command implementations
    # ------------------------------------------------------------------

    def _cmd_help(self) -> None:
        self.console.print(Panel(
            "/help           — Show this help\n"
            "/exit           — Exit the agent\n"
            "/clear          — Clear conversation history\n"
            "/tools          — List available tools\n"
            "/servers        — Show MCP server status\n"
            "/memory         — Show memory directories\n"
            "/agents         — List available agent presets\n"
            "/agent <name>   — Switch to a different agent preset\n"
            "/skills         — List available skills",
            title="Commands",
        ))

    def _cmd_tools(self) -> None:
        tools = self.tools.to_openai_tools()
        self.console.print(f"[info]Available tools ({len(tools)}):[/info]")
        for t in tools:
            fn = t["function"]
            self.console.print(f"  • {fn['name']}: {_truncate(fn.get('description', ''), 80)}")

    def _cmd_memory(self) -> None:
        summary = self.agent.memory.load_user_memory_summary(max_lines=50)
        if summary:
            self.console.print(Panel(summary, title="User Memory"))
        else:
            self.console.print("[info]No user memory files found.[/info]")

    def _cmd_agents(self) -> None:
        loader = self.agent.preset_loader
        if not loader:
            self.console.print("[info]Agent presets not configured.[/info]")
            return

        presets = loader.get_all()
        active_name = loader.active.name
        self.console.print(f"[info]Agent presets ({len(presets)}):[/info]")
        for p in sorted(presets, key=lambda x: x.name):
            marker = " [agent.name]← active[/agent.name]" if p.name == active_name else ""
            desc = f" — {p.description}" if p.description else ""
            self.console.print(f"  • [bold]{p.name}[/bold]{desc}{marker}")

    def _cmd_agent_switch(self, name: str) -> None:
        if not name:
            # No argument — show current agent
            self.console.print(
                f"[info]Active agent:[/info] [agent.name]{self.agent.active_agent_name}[/agent.name]"
            )
            self.console.print("[info]Use /agent <name> to switch. /agents to list all.[/info]")
            return

        result = self.agent.switch_preset(name)
        if result:
            self.console.print(
                f"[info]Switched to agent:[/info] [agent.name]{result}[/agent.name] "
                f"(conversation cleared)"
            )
        else:
            self.console.print(f"[error]Agent '{name}' not found. Use /agents to list available.[/error]")

    def _cmd_skills(self) -> None:
        if not self.skill_loader:
            self.console.print("[info]Skills not configured.[/info]")
            return

        skills = self.skill_loader.get_all()
        if not skills:
            self.console.print("[info]No skills found in skills directory.[/info]")
            return

        self.console.print(f"[info]Available skills ({len(skills)}):[/info]")
        for s in sorted(skills, key=lambda x: x.name):
            desc = f" — {s.description}" if s.description else ""
            triggers = f" [dim](triggers: {', '.join(s.triggers)})[/dim]" if s.triggers else ""
            self.console.print(f"  • [bold]{s.name}[/bold]{desc}{triggers}")

    # ------------------------------------------------------------------
    # Status display
    # ------------------------------------------------------------------

    def _print_status(self) -> None:
        # Active agent
        self.console.print(
            f"[info]Agent:[/info] [agent.name]{self.agent.active_agent_name}[/agent.name]"
        )

        if self.mcp:
            servers = self.mcp.get_server_names()
            if servers:
                lines = []
                for s in servers:
                    count = self.mcp.get_server_tool_count(s)
                    lines.append(f"  ✓ {s} ({count} tools)")
                self.console.print("[info]MCP Servers:[/info]")
                for line in lines:
                    self.console.print(line)
            else:
                self.console.print("[info]No MCP servers connected.[/info]")
        else:
            self.console.print("[info]MCP: disabled (no mcp.json)[/info]")

        tool_count = len(self.tools.to_openai_tools())
        self.console.print(f"[info]Total tools: {tool_count}[/info]")

        # Skills summary
        if self.skill_loader:
            skill_count = len(self.skill_loader.get_all())
            if skill_count:
                self.console.print(f"[info]Skills: {skill_count}[/info]")

        # Agent presets summary
        if self.agent.preset_loader:
            preset_count = len(self.agent.preset_loader.get_names())
            if preset_count > 1:
                self.console.print(f"[info]Agent presets: {preset_count} (use /agents to list)[/info]")


def _truncate(s: str, max_len: int) -> str:
    s = s.replace("\n", " ")
    if len(s) > max_len:
        return s[: max_len - 3] + "..."
    return s
