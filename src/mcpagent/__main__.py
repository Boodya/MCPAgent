"""MCPAgent entry point — async main, config loading, graceful shutdown."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def main_entry() -> None:
    """Synchronous entry point for the console script."""
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass
    except (RuntimeError, BaseExceptionGroup):
        # Suppress anyio/MCP cancel scope errors during shutdown
        pass


async def async_main() -> None:
    from mcpagent.agent_presets import AgentPresetLoader
    from mcpagent.config import load_app_config, AppConfig
    from mcpagent.llm import LLMClient
    from mcpagent.mcp_manager import MCPManager
    from mcpagent.memory import MemoryManager
    from mcpagent.skills import SkillLoader
    from mcpagent.storage import StorageManager
    from mcpagent.tools import ToolRegistry
    from mcpagent.agent import Agent
    from mcpagent.cli import CLI

    # --- Logging ---
    log_level = os.environ.get("MCPAGENT_LOG_LEVEL", "WARNING").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.WARNING),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # --- Load .env ---
    env_path = Path(".env")
    if env_path.exists():
        load_dotenv(env_path)

    # --- Config ---
    config_dir = Path(os.environ.get("MCPAGENT_CONFIG_DIR", "config"))
    if not config_dir.exists():
        # Fallback: look next to the package
        alt = Path(__file__).parent.parent.parent / "config"
        if alt.exists():
            config_dir = alt

    config = load_app_config(config_dir)

    # --- LLM ---
    model_cfg = config.models.get(config.default_model)
    if not model_cfg:
        print(f"Error: model '{config.default_model}' not found in config.yaml", file=sys.stderr)
        sys.exit(1)

    try:
        llm = LLMClient(model_cfg)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    # --- Storage (data_dir) ---
    data_dir = Path(config.storage.data_dir).resolve()

    # --- Memory ---
    memory = MemoryManager(data_dir=data_dir)

    # --- Storage manager (history + logs) ---
    storage = StorageManager(
        data_dir=data_dir,
        save_history=config.storage.chat_history,
        save_logs=config.storage.logs,
    )

    # --- MCP (optional) ---
    mcp_mgr: MCPManager | None = None
    if config.mcp.servers:
        mcp_mgr = MCPManager(config.mcp.servers)

    agent: Agent | None = None

    try:
        if mcp_mgr:
            server_names = ", ".join(config.mcp.servers.keys())
            print(f"  Connecting MCP servers: {server_names} ...", end=" ", flush=True)
            await mcp_mgr.__aenter__()
            connected = len(mcp_mgr._connections)
            total = len(config.mcp.servers)
            print(f"done ({connected}/{total})")

        # --- Tools ---
        tools = ToolRegistry(memory=memory, mcp=mcp_mgr, tools_config=config.tools)

        # --- Skills ---
        skills_dir = Path(config.skills_dir)
        skill_loader = SkillLoader(skills_dir)

        # --- Agent Presets ---
        agents_dir = Path(config.agents_dir)
        preset_loader = AgentPresetLoader(agents_dir)

        # --- Agent ---
        agent = Agent(
            llm=llm, tools=tools, memory=memory,
            config=config.agent, storage=storage,
            preset_loader=preset_loader,
            skill_loader=skill_loader,
        )

        # --- CLI ---
        cli = CLI(
            agent=agent, tools=tools, mcp=mcp_mgr,
            storage=storage, skill_loader=skill_loader,
        )
        await cli.run()

    finally:
        # Save chat history + flush logs
        if agent:
            storage.save_chat(agent.messages)
            storage.log_event("session_end")
            storage.flush_logs()

        if mcp_mgr:
            try:
                await mcp_mgr.__aexit__(None, None, None)
            except (RuntimeError, BaseExceptionGroup, Exception):
                # MCP SDK uses anyio task groups that can raise RuntimeError
                # ("Attempted to exit cancel scope in a different task")
                # on shutdown. This is harmless — connections are dead anyway.
                pass
        await llm.close()
        memory.cleanup_session()


if __name__ == "__main__":
    main_entry()
