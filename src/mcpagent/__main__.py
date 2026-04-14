"""MCPAgent entry point — subcommands: chat, run, job, scheduler."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def main_entry() -> None:
    """Synchronous entry point for the console script."""
    parser = argparse.ArgumentParser(prog="mcpagent", description="Universal AI agent with MCP tool integration")
    sub = parser.add_subparsers(dest="command")

    # --- chat (default — interactive REPL) ---
    sub.add_parser("chat", help="Interactive chat session (default)")

    # --- run (headless one-shot) ---
    run_p = sub.add_parser("run", help="Run agent headlessly with a single message")
    run_p.add_argument("--agent", "-a", default=None, help="Agent preset name")
    run_p.add_argument("--message", "-m", required=True, help="Message to send to the agent")

    # --- job ---
    job_p = sub.add_parser("job", help="Workflow job management")
    job_sub = job_p.add_subparsers(dest="job_command")

    job_run_p = job_sub.add_parser("run", help="Run a workflow by name")
    job_run_p.add_argument("name", help="Workflow name")

    job_sub.add_parser("list", help="List available workflows")

    job_hist_p = job_sub.add_parser("history", help="Show run history")
    job_hist_p.add_argument("name", nargs="?", default=None, help="Filter by workflow name")
    job_hist_p.add_argument("--limit", "-n", type=int, default=20, help="Max rows")

    job_status_p = job_sub.add_parser("status", help="Show status of a specific run")
    job_status_p.add_argument("run_id", type=int, help="Run ID")

    # --- scheduler ---
    sched_p = sub.add_parser("scheduler", help="Workflow scheduler daemon")
    sched_sub = sched_p.add_subparsers(dest="sched_command")
    sched_sub.add_parser("start", help="Start the scheduler daemon")
    sched_sub.add_parser("status", help="Show scheduled workflows")

    args = parser.parse_args()
    command = args.command

    # Default to chat if no subcommand
    if command is None:
        command = "chat"

    try:
        if command == "chat":
            asyncio.run(_cmd_chat())
        elif command == "run":
            asyncio.run(_cmd_run(agent=args.agent, message=args.message))
        elif command == "job":
            asyncio.run(_cmd_job(args))
        elif command == "scheduler":
            asyncio.run(_cmd_scheduler(args))
        else:
            parser.print_help()
    except KeyboardInterrupt:
        pass
    except (RuntimeError, BaseExceptionGroup):
        pass


# -------------------------------------------------------------------------
# Subcommand: chat (interactive REPL — original behaviour)
# -------------------------------------------------------------------------

async def _cmd_chat() -> None:
    from mcpagent.agent_presets import AgentPresetLoader
    from mcpagent.background import BackgroundManager
    from mcpagent.config import load_app_config
    from mcpagent.db import JobStore
    from mcpagent.llm import LLMClient
    from mcpagent.mcp_manager import MCPManager
    from mcpagent.memory import MemoryManager
    from mcpagent.ops_log import OpsLog
    from mcpagent.skills import SkillLoader
    from mcpagent.storage import StorageManager
    from mcpagent.tools import ToolRegistry
    from mcpagent.workflow_engine import WorkflowEngine
    from mcpagent.workflow_models import WorkflowLoader
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
        alt = Path(__file__).parent.parent.parent / "config"
        if alt.exists():
            config_dir = alt

    config = load_app_config(config_dir)

    # --- Storage (data_dir) ---
    data_dir = Path(config.storage.data_dir).resolve()

    # --- Ops log ---
    ops = OpsLog(data_dir)

    # Redirect Python logging to file so background task errors
    # don't leak tracebacks into the interactive console
    _log_dir = data_dir / "logs"
    _log_dir.mkdir(parents=True, exist_ok=True)
    _file_handler = logging.FileHandler(_log_dir / "mcpagent.log", encoding="utf-8")
    _file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root_logger = logging.getLogger()
    for h in root_logger.handlers[:]:
        root_logger.removeHandler(h)
    root_logger.addHandler(_file_handler)

    # --- LLM ---
    model_cfg = config.models.get(config.default_model)
    if not model_cfg:
        print(f"Error: model '{config.default_model}' not found in config.yaml", file=sys.stderr)
        sys.exit(1)

    try:
        llm = LLMClient(model_cfg, ops=ops)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

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
        mcp_mgr = MCPManager(config.mcp.servers, ops=ops)

    agent: Agent | None = None
    bg_mgr: BackgroundManager | None = None

    try:
        # --- Tools ---
        tools = ToolRegistry(memory=memory, mcp=mcp_mgr, tools_config=config.tools)

        # --- Skills ---
        skills_dir = Path(config.skills_dir)
        if not skills_dir.is_absolute():
            candidate = config_dir.parent / config.skills_dir
            if candidate.is_dir():
                skills_dir = candidate
        skill_loader = SkillLoader(skills_dir)

        # --- Agent Presets ---
        agents_dir = Path(config.agents_dir)
        if not agents_dir.is_absolute():
            candidate = config_dir.parent / config.agents_dir
            if candidate.is_dir():
                agents_dir = candidate
        preset_loader = AgentPresetLoader(agents_dir)

        # Switch to the configured default agent
        if config.default_agent != "default":
            switched = preset_loader.switch(config.default_agent)
            if not switched:
                print(f"Warning: default_agent '{config.default_agent}' not found, using 'default'", file=sys.stderr)

        # --- Start MCP servers for the default agent ---
        if mcp_mgr:
            default_preset = preset_loader.active
            desired = default_preset.mcp_servers  # None = all
            if desired is None:
                server_label = "all"
                server_names = ", ".join(config.mcp.servers.keys())
            else:
                server_label = f"{len(desired)} of {len(config.mcp.servers)}"
                server_names = ", ".join(desired) if desired else "(none)"

            if desired is None or desired:
                print(f"  Connecting MCP servers ({server_label}): {server_names} ...", end=" ", flush=True)
                started, _ = await mcp_mgr.ensure_servers(desired)
                connected = len(mcp_mgr.get_server_names())
                total = len(desired) if desired is not None else len(config.mcp.servers)
                print(f"done ({connected}/{total})")

        # --- Background workflow manager (optional) ---
        workflows_dir = Path(config.workflows_dir)
        if not workflows_dir.is_absolute():
            candidate = config_dir.parent / config.workflows_dir
            if candidate.is_dir():
                workflows_dir = candidate
        if workflows_dir.is_dir():
            db = JobStore(data_dir / "mcpagent.db")
            await db.init_db()
            engine = WorkflowEngine(db, ops=ops)
            wf_loader = WorkflowLoader(workflows_dir)
            bg_mgr = BackgroundManager(engine, wf_loader)

        # --- Agent ---
        platform_paths = {
            "agents_dir": str(agents_dir),
            "skills_dir": str(skills_dir),
            "workflows_dir": str(workflows_dir),
            "data_dir": str(data_dir),
        }
        agent = Agent(
            llm=llm, tools=tools, memory=memory,
            config=config.agent, storage=storage,
            preset_loader=preset_loader,
            skill_loader=skill_loader,
            mcp_manager=mcp_mgr,
            background=bg_mgr,
            ops=ops,
            platform_paths=platform_paths,
        )

        # --- CLI ---
        cli = CLI(
            agent=agent, tools=tools, mcp=mcp_mgr,
            storage=storage, skill_loader=skill_loader,
            config_dir=config_dir,
            background=bg_mgr,
        )
        await cli.run()

    finally:
        # Cancel background tasks on exit
        if bg_mgr:
            try:
                await asyncio.wait_for(bg_mgr.shutdown(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

        # Save chat history + flush logs
        if agent:
            storage.save_chat(agent.messages)
            storage.log_event("session_end")
            storage.flush_logs()

        if mcp_mgr:
            try:
                await mcp_mgr.shutdown()
            except (RuntimeError, BaseExceptionGroup, asyncio.TimeoutError, Exception):
                pass
        try:
            await asyncio.wait_for(llm.close(), timeout=3.0)
        except asyncio.TimeoutError:
            pass
        memory.cleanup_session()

        # Force-exit to avoid hanging on orphan threads (e.g. stdin reader)
        os._exit(0)


# -------------------------------------------------------------------------
# Subcommand: run (headless one-shot)
# -------------------------------------------------------------------------

async def _cmd_run(agent: str | None, message: str) -> None:
    log_level = os.environ.get("MCPAGENT_LOG_LEVEL", "WARNING").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.WARNING),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from mcpagent.headless import create_agent

    ag, cleanup = await create_agent(agent_name=agent)  # ops created inside headless
    try:
        result = await ag.run_to_completion(message)
        if result.text:
            print(result.text)
        if result.error:
            print(f"Error: {result.error}", file=sys.stderr)
            sys.exit(1)
    finally:
        if ag.storage:
            ag.storage.save_chat(ag.messages)
            ag.storage.log_event("session_end")
            ag.storage.flush_logs()
        await cleanup()


# -------------------------------------------------------------------------
# Subcommand: job
# -------------------------------------------------------------------------

async def _cmd_job(args: argparse.Namespace) -> None:
    log_level = os.environ.get("MCPAGENT_LOG_LEVEL", "WARNING").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.WARNING),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from mcpagent.config import load_app_config
    from mcpagent.db import JobStore
    from mcpagent.ops_log import OpsLog
    from mcpagent.workflow_models import WorkflowLoader
    from mcpagent.workflow_engine import WorkflowEngine

    config_dir = Path(os.environ.get("MCPAGENT_CONFIG_DIR", "config"))
    if not config_dir.exists():
        alt = Path(__file__).parent.parent.parent / "config"
        if alt.exists():
            config_dir = alt
    config = load_app_config(config_dir)

    data_dir = Path(config.storage.data_dir).resolve()
    ops = OpsLog(data_dir)
    db = JobStore(data_dir / "mcpagent.db")
    await db.init_db()

    workflows_dir = Path(config.workflows_dir)
    if not workflows_dir.is_absolute():
        candidate = config_dir.parent / config.workflows_dir
        if candidate.is_dir():
            workflows_dir = candidate

    cmd = args.job_command

    if cmd == "list":
        loader = WorkflowLoader(workflows_dir)
        workflows = loader.load_all()
        if not workflows:
            print("No workflows found.")
            return
        print(f"{'Name':<25} {'Schedule':<20} {'Enabled':<8} Steps")
        print("-" * 70)
        for wf in workflows:
            sched = wf.schedule or (f"every {wf.interval}s" if wf.interval else "manual")
            print(f"{wf.name:<25} {sched:<20} {'yes' if wf.enabled else 'no':<8} {len(wf.steps)}")

    elif cmd == "run":
        loader = WorkflowLoader(workflows_dir)
        workflows = {wf.name: wf for wf in loader.load_all()}
        wf = workflows.get(args.name)
        if not wf:
            print(f"Workflow '{args.name}' not found. Available: {list(workflows.keys())}", file=sys.stderr)
            sys.exit(1)
        engine = WorkflowEngine(db, ops=ops)
        run_result = await engine.run_workflow(wf)
        print(f"\nWorkflow '{wf.name}' — {run_result.status}")
        if run_result.error:
            print(f"Error: {run_result.error}")
        for sr in run_result.step_results:
            status_icon = {"completed": "✓", "failed": "✗", "skipped": "⊘"}.get(sr.status, "?")
            print(f"  {status_icon} {sr.step_id}: {sr.status}")

    elif cmd == "history":
        runs = await db.list_runs(workflow_name=args.name, limit=args.limit)
        if not runs:
            print("No runs found.")
            return
        print(f"{'ID':<6} {'Workflow':<25} {'Status':<12} {'Started':<22} {'Finished':<22}")
        print("-" * 90)
        for r in runs:
            print(f"{r['id']:<6} {r['workflow_name']:<25} {r['status']:<12} {r['started_at'] or '':<22} {r['finished_at'] or '':<22}")

    elif cmd == "status":
        run = await db.get_run(args.run_id)
        if not run:
            print(f"Run {args.run_id} not found.", file=sys.stderr)
            sys.exit(1)
        print(f"Run #{run['id']}: {run['workflow_name']} — {run['status']}")
        if run.get("error"):
            print(f"Error: {run['error']}")
        steps = await db.get_step_results(args.run_id)
        for s in steps:
            status_icon = {"completed": "✓", "failed": "✗", "skipped": "⊘", "running": "…"}.get(s["status"], "?")
            print(f"  {status_icon} {s['step_id']}: {s['status']} (agent: {s['agent_name']})")
            if s.get("error"):
                print(f"    Error: {s['error']}")

    else:
        print("Usage: mcpagent job {list|run|history|status}", file=sys.stderr)
        sys.exit(1)

    await db.close()


# -------------------------------------------------------------------------
# Subcommand: scheduler
# -------------------------------------------------------------------------

async def _cmd_scheduler(args: argparse.Namespace) -> None:
    log_level = os.environ.get("MCPAGENT_LOG_LEVEL", "WARNING").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.WARNING),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    cmd = args.sched_command

    if cmd == "start":
        from mcpagent.scheduler import SchedulerService
        service = SchedulerService()
        await service.start()

    elif cmd == "status":
        from mcpagent.scheduler import SchedulerService
        service = SchedulerService()
        await service.show_status()

    else:
        print("Usage: mcpagent scheduler {start|status}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main_entry()
