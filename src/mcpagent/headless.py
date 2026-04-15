"""Headless agent factory — create agents programmatically without CLI."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, Coroutine

from dotenv import load_dotenv

log = logging.getLogger(__name__)


async def create_agent(
    config_dir: str | Path | None = None,
    agent_name: str | None = None,
    ops: "OpsLog | None" = None,
) -> tuple["Agent", Callable[[], Coroutine[Any, Any, None]]]:
    """Create a fully-initialised Agent without starting the interactive CLI.

    Returns ``(agent, cleanup)`` where *cleanup* is an async callable that
    shuts down MCP servers and the LLM client.  The caller **must** await
    ``cleanup()`` when done.
    """
    from mcpagent.agent import Agent
    from mcpagent.agent_presets import AgentPresetLoader
    from mcpagent.config import load_app_config, resolve_dirs
    from mcpagent.llm import LLMClient
    from mcpagent.mcp_manager import MCPManager
    from mcpagent.memory import MemoryManager
    from mcpagent.ops_log import OpsLog
    from mcpagent.skills import SkillLoader
    from mcpagent.storage import StorageManager
    from mcpagent.tools import ToolRegistry

    # --- .env ---
    env_path = Path(".env")
    if env_path.exists():
        load_dotenv(env_path)

    # --- Config dir ---
    config_dir, base_dir = resolve_dirs(config_dir)

    config = load_app_config(config_dir)

    # --- LLM ---
    model_cfg = config.models.get(config.default_model)
    if not model_cfg:
        raise ValueError(f"Model '{config.default_model}' not found in config.yaml")
    llm = LLMClient(model_cfg, ops=ops)

    # --- Storage ---
    data_dir = Path(config.storage.data_dir)
    if not data_dir.is_absolute():
        data_dir = (base_dir / data_dir).resolve()
    else:
        data_dir = data_dir.resolve()
    memory = MemoryManager(data_dir=data_dir)
    storage = StorageManager(
        data_dir=data_dir,
        save_history=config.storage.chat_history,
        save_logs=config.storage.logs,
    )

    # --- MCP ---
    mcp_mgr: MCPManager | None = None
    if config.mcp.servers:
        mcp_mgr = MCPManager(config.mcp.servers, ops=ops)

    # --- Tools ---
    tools = ToolRegistry(memory=memory, mcp=mcp_mgr, tools_config=config.tools)

    # --- Skills ---
    skills_dir = Path(config.skills_dir)
    if not skills_dir.is_absolute():
        candidate = base_dir / config.skills_dir
        if candidate.is_dir():
            skills_dir = candidate
    skill_loader = SkillLoader(skills_dir)

    # --- Agent presets ---
    agents_dir = Path(config.agents_dir)
    if not agents_dir.is_absolute():
        candidate = base_dir / config.agents_dir
        if candidate.is_dir():
            agents_dir = candidate
    preset_loader = AgentPresetLoader(agents_dir)

    target_name = agent_name or config.default_agent
    if target_name != "default":
        if not preset_loader.switch(target_name):
            log.warning("Agent '%s' not found, using 'default'", target_name)

    # --- Start MCP servers for the agent ---
    if mcp_mgr:
        desired = preset_loader.active.mcp_servers
        if desired is None or desired:
            await mcp_mgr.ensure_servers(desired)

    # --- Agent ---
    platform_paths = {
        "agents_dir": str(agents_dir),
        "skills_dir": str(skills_dir),
        "data_dir": str(data_dir),
    }
    agent = Agent(
        llm=llm,
        tools=tools,
        memory=memory,
        config=config.agent,
        storage=storage,
        preset_loader=preset_loader,
        skill_loader=skill_loader,
        mcp_manager=mcp_mgr,
        ops=ops,
        platform_paths=platform_paths,
    )

    async def cleanup() -> None:
        if mcp_mgr:
            try:
                await mcp_mgr.shutdown()
            except (RuntimeError, BaseExceptionGroup, Exception):
                pass
        await llm.close()
        memory.cleanup_session()

    return agent, cleanup
