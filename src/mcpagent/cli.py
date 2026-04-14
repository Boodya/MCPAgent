"""Rich CLI interface — streaming output, tool call display, slash commands."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from rich.theme import Theme

if TYPE_CHECKING:
    from mcpagent.agent import Agent, AgentEvent
    from mcpagent.background import BackgroundManager
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
        config_dir: Path | None = None,
        background: BackgroundManager | None = None,
    ) -> None:
        self.agent = agent
        self.tools = tools
        self.mcp = mcp
        self.storage = storage
        self.skill_loader = skill_loader
        self.config_dir = config_dir
        self.background = background
        self.console = Console(theme=THEME)
        self._pending_bg_events: list = []

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
                user_input = await self._wait_for_input()
            except (EOFError, KeyboardInterrupt):
                self.console.print("\n[info]Goodbye![/info]")
                break

            if user_input is None:
                # Only background events were processed, no user input
                continue

            if not user_input.strip():
                continue

            # Slash commands
            if user_input.startswith("/"):
                should_continue = await self._handle_command(user_input.strip())
                if not should_continue:
                    break
                continue

            # Run agent
            await self._run_agent(user_input)

    async def _wait_for_input(self) -> str | None:
        """Wait for user input while monitoring background events.

        If a background workflow completes while waiting, a proactive
        notification is printed and the agent processes the result inline.
        Returns the user''s input string, or raises EOFError/KeyboardInterrupt.
        """
        input_future = asyncio.ensure_future(asyncio.to_thread(self._get_input))

        while not input_future.done():
            # Wait on the input future with a short timeout
            done, _ = await asyncio.wait({input_future}, timeout=0.5)
            if done:
                break

            # Check for background events
            if self.background:
                try:
                    event = self.background.events.get_nowait()
                except asyncio.QueueEmpty:
                    continue

                # Proactive notification
                status_style = "bold green" if event.status == "completed" else "bold red"
                self.console.print(
                    f"\n  [{status_style}]🔔 Background: "
                    f"{event.workflow_name} — {event.status}[/{status_style}]"
                )

                # Run agent to process and report the result
                notification = (
                    f"[BACKGROUND WORKFLOW NOTIFICATION]\n"
                    f"A background workflow has finished. Report the results to the user.\n\n"
                    f"Task ID: {event.task_id}\n"
                    f"Workflow: {event.workflow_name}\n"
                    f"Status: {event.status}\n"
                    f"Details:\n{event.summary}"
                )
                await self._run_agent(notification)

        return input_future.result()

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
                    # Special display for skill loading
                    if event.tool_name == "load_skill":
                        skill_name = event.tool_args.get("name", "?")
                        self.console.print(
                            f"  [bold yellow]📚 Loading skill:[/bold yellow] [bold]{skill_name}[/bold]"
                        )
                    elif event.tool_name == "call_agent":
                        agent_name = event.tool_args.get("name", "?")
                        msg_preview = _truncate(event.tool_args.get("message", ""), 100)
                        self.console.print(
                            f"  [bold magenta]🤖 Calling agent:[/bold magenta] [bold]{agent_name}[/bold] — {msg_preview}"
                        )
                    else:
                        args_str = _truncate(str(event.tool_args), 200)
                        self.console.print(
                            f"  [tool.name]⚡ {event.tool_name}[/tool.name]({args_str})"
                        )

                elif event.type == "tool_result":
                    # Skip verbose output for skill loading
                    if event.tool_name == "load_skill":
                        if "already_loaded" in event.content:
                            self.console.print(f"  [dim]→ already loaded[/dim]")
                        else:
                            self.console.print(f"  [bold yellow]→ loaded[/bold yellow]")
                    elif event.tool_name == "call_agent":
                        result_preview = _truncate(event.content, 500)
                        self.console.print(f"  [bold magenta]→ {result_preview}[/bold magenta]")
                    else:
                        result_preview = _truncate(event.content, 300)
                        self.console.print(f"  [tool.result]→ {result_preview}[/tool.result]")

                elif event.type == "context_summarizing":
                    self.console.print(f"[bold blue]📝 {event.content}[/bold blue]")

                elif event.type == "context_summarized":
                    self.console.print(f"[bold blue]✓ {event.content}[/bold blue]")

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

    async def _handle_command(self, cmd: str) -> bool:
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
            await self._cmd_agent_switch(arg)
            return True

        if command == "/skills":
            self._cmd_skills()
            return True

        if command == "/context":
            self._cmd_context()
            return True

        if command == "/reload":
            await self._cmd_reload()
            return True

        if command == "/bg":
            self._cmd_bg()
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
            "/skills         — List available skills\n"
            "/context        — Show context window usage\n"
            "/bg             — Show background workflow tasks\n"
            "/reload         — Reload agents, skills, and MCP config from disk",
            title="Commands",
        ))

    def _cmd_bg(self) -> None:
        if not self.background:
            self.console.print("[info]Background workflows not available (no workflows configured).[/info]")
            return
        tasks = self.background.get_tasks()
        if not tasks:
            self.console.print("[info]No background tasks.[/info]")
            return
        self.console.print(f"[info]Background tasks ({len(tasks)}):[/info]")
        for t in tasks:
            icon = {"running": "⏳", "completed": "✓", "failed": "✗", "cancelled": "⊘"}.get(t.status, "?")
            elapsed = ""
            if t.finished_at:
                delta = t.finished_at - t.started_at
                elapsed = f" ({delta.total_seconds():.1f}s)"
            self.console.print(f"  {icon} {t.id}: {t.workflow_name} — {t.status}{elapsed}")

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
            # Show MCP servers config
            if p.mcp_servers is None:
                mcp_info = "all"
            elif p.mcp_servers:
                mcp_info = ", ".join(p.mcp_servers)
            else:
                mcp_info = "none"
            extras = f" [dim](mcp: {mcp_info})[/dim]"
            if p.subagents:
                extras += f" [dim](subagents: {', '.join(p.subagents)})[/dim]"
            self.console.print(f"  • [bold]{p.name}[/bold]{desc}{extras}{marker}")

    async def _cmd_agent_switch(self, name: str) -> None:
        if not name:
            # No argument — show current agent
            self.console.print(
                f"[info]Active agent:[/info] [agent.name]{self.agent.active_agent_name}[/agent.name]"
            )
            self.console.print("[info]Use /agent <name> to switch. /agents to list all.[/info]")
            return

        self.console.print(f"[dim]Switching to agent '{name}'...[/dim]")
        result = await self.agent.switch_preset(name)
        if result:
            # Report MCP server state
            if self.mcp:
                servers = self.mcp.get_server_names()
                if servers:
                    srv_info = ", ".join(servers)
                    self.console.print(f"[dim]  MCP servers active: {srv_info}[/dim]")
                else:
                    self.console.print("[dim]  No MCP servers active[/dim]")
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

    def _cmd_context(self) -> None:
        """Show context window usage stats."""
        ctx = self.agent.ctx
        # Recount tokens fresh
        ctx.count_tokens(self.agent.messages)
        stats = ctx.get_stats()

        bar_width = 30
        filled = int(bar_width * stats["usage_pct"] / 100)
        bar = "█" * filled + "░" * (bar_width - filled)

        color = "green"
        if stats["usage_pct"] > 70:
            color = "yellow"
        if stats["usage_pct"] > 90:
            color = "red"

        self.console.print(f"[info]Context window:[/info]")
        self.console.print(
            f"  [{color}]{bar}[/{color}] "
            f"{stats['tokens']:,} / {stats['context_window']:,} tokens "
            f"({stats['usage_pct']}%)"
        )
        self.console.print(f"  Messages: {len(self.agent.messages)}")
        self.console.print(
            f"  Summarization threshold: {stats['threshold']:,} tokens "
            f"({int(ctx.summarize_threshold * 100)}%)"
        )
        if stats["summarizations"] > 0:
            self.console.print(f"  Summarizations performed: {stats['summarizations']}")

    async def _cmd_reload(self) -> None:
        """Reload agents, skills, and MCP config from disk."""
        from mcpagent.config import load_mcp_config

        # Reload agents
        if self.agent.preset_loader:
            count = self.agent.preset_loader.reload()
            self.console.print(f"  [info]Agents:[/info] {count} loaded")

        # Reload skills
        if self.skill_loader:
            count = self.skill_loader.reload()
            self.console.print(f"  [info]Skills:[/info] {count} loaded")

        # Reload MCP config
        if self.mcp and self.config_dir:
            mcp_path = self.config_dir / "mcp.json"
            mcp_cfg = load_mcp_config(mcp_path)
            if mcp_cfg.servers:
                added, removed = await self.mcp.reload_config(mcp_cfg.servers)
                total = len(self.mcp.get_available_server_names())
                msg = f"  [info]MCP servers:[/info] {total} configured"
                if added:
                    msg += f" [dim](+{', '.join(added)})[/dim]"
                if removed:
                    msg += f" [dim](-{', '.join(removed)})[/dim]"
                self.console.print(msg)
            else:
                self.console.print("  [info]MCP servers:[/info] none in mcp.json")

        self.console.print("[info]Reload complete.[/info]")

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
                self.console.print(f"[info]Skills: {skill_count}[/info] (from {self.skill_loader.skills_dir})")
            else:
                self.console.print(f"[info]Skills: 0[/info] [dim](dir: {self.skill_loader.skills_dir})[/dim]")

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
