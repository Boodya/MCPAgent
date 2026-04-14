"""Configuration models and loaders for MCPAgent."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ModelConfig(BaseModel):
    """Azure OpenAI model configuration."""

    provider: str = "azure"
    endpoint: str = ""
    deployment: str = ""
    api_version: str = "2024-12-01-preview"
    max_tokens: int = 4096
    temperature: float = 0.1


class StorageConfig(BaseModel):
    """All persistent storage paths. Relative paths resolve from working directory."""

    data_dir: str = ".mcpagent"           # root for all agent data
    chat_history: bool = True             # save chat history
    logs: bool = True                     # save execution logs


class AgentConfig(BaseModel):
    """Default agent settings."""

    system_prompt: str = ""
    max_iterations: int = 30
    model: str = "default"


class BuiltinToolConfig(BaseModel):
    """Configuration for a single built-in tool."""

    enabled: bool = True
    description: str = ""  # override default description; empty = use default


class RunCommandToolConfig(BuiltinToolConfig):
    """Extended config for run_command."""

    timeout: int = 60
    confirm: bool = False  # if True, agent must get user confirmation before running


class GrepSearchToolConfig(BuiltinToolConfig):
    """Extended config for grep_search."""

    max_results: int = 200


class ReadFileToolConfig(BuiltinToolConfig):
    """Extended config for read_file."""

    max_size_kb: int = 512  # max file size to read (in KB)


class ToolsConfig(BaseModel):
    """Configuration for all built-in tools."""

    read_file: ReadFileToolConfig = Field(default_factory=ReadFileToolConfig)
    write_file: BuiltinToolConfig = Field(default_factory=BuiltinToolConfig)
    list_dir: BuiltinToolConfig = Field(default_factory=BuiltinToolConfig)
    grep_search: GrepSearchToolConfig = Field(default_factory=GrepSearchToolConfig)
    run_command: RunCommandToolConfig = Field(default_factory=RunCommandToolConfig)
    memory_view: BuiltinToolConfig = Field(default_factory=BuiltinToolConfig)
    memory_create: BuiltinToolConfig = Field(default_factory=BuiltinToolConfig)
    memory_update: BuiltinToolConfig = Field(default_factory=BuiltinToolConfig)
    memory_delete: BuiltinToolConfig = Field(default_factory=BuiltinToolConfig)


class McpServerConfig(BaseModel):
    """Single MCP server entry (VS Code mcp.json compatible)."""

    type: str = "stdio"  # "stdio" | "http"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    tools: list[str] | None = None  # optional tool filter


class McpConfig(BaseModel):
    """Parsed mcp.json."""

    servers: dict[str, McpServerConfig] = Field(default_factory=dict)


class AppConfig(BaseModel):
    """Root application configuration."""

    models: dict[str, ModelConfig] = Field(default_factory=lambda: {"default": ModelConfig()})
    default_model: str = "default"
    agent: AgentConfig = Field(default_factory=AgentConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    skills_dir: str = "skills"
    agents_dir: str = "agents"
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)


# ---------------------------------------------------------------------------
# Placeholder resolution  ${input:NAME} / ${env:NAME} → env vars / .env
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\$\{(?:input|env):([^}]+)\}")


def _resolve_placeholders(value: str) -> str:
    """Replace ${input:NAME} and ${env:NAME} placeholders with env var values."""

    def _replacer(m: re.Match) -> str:
        var_name = m.group(1)
        env_val = os.environ.get(var_name, "")
        return env_val

    return _PLACEHOLDER_RE.sub(_replacer, value)


def _resolve_dict(d: dict[str, Any]) -> dict[str, Any]:
    """Recursively resolve placeholders in a dict."""
    resolved: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, str):
            resolved[k] = _resolve_placeholders(v)
        elif isinstance(v, dict):
            resolved[k] = _resolve_dict(v)
        elif isinstance(v, list):
            resolved[k] = [_resolve_placeholders(i) if isinstance(i, str) else i for i in v]
        else:
            resolved[k] = v
    return resolved


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _strip_json_comments(text: str) -> str:
    """Remove // and /* */ comments and trailing commas from JSON text (JSONC support)."""
    result: list[str] = []
    i = 0
    in_string = False
    while i < len(text):
        ch = text[i]
        # Handle string literals (skip comment detection inside strings)
        if ch == '"' and (i == 0 or text[i - 1] != '\\'):
            in_string = not in_string
            result.append(ch)
            i += 1
        elif not in_string and text[i:i+2] == '//':
            # Line comment — skip until newline
            i = text.find('\n', i)
            if i == -1:
                break
        elif not in_string and text[i:i+2] == '/*':
            # Block comment — skip until */
            end = text.find('*/', i + 2)
            i = end + 2 if end != -1 else len(text)
        else:
            result.append(ch)
            i += 1
    # Strip trailing commas before } or ]
    import re as _re
    cleaned = _re.sub(r',\s*([}\]])', r'\1', ''.join(result))
    return cleaned


def load_mcp_config(path: str | Path) -> McpConfig:
    """Load and parse an mcp.json file (VS Code JSONC format — comments allowed)."""
    path = Path(path)
    if not path.exists():
        return McpConfig()

    text = path.read_text(encoding="utf-8")
    raw = json.loads(_strip_json_comments(text))
    servers_raw: dict[str, Any] = raw.get("servers", {})

    servers: dict[str, McpServerConfig] = {}
    for name, cfg in servers_raw.items():
        resolved = _resolve_dict(cfg)
        servers[name] = McpServerConfig(**resolved)

    return McpConfig(servers=servers)


def _env_override_model(cfg: ModelConfig) -> ModelConfig:
    """Override ModelConfig fields from environment variables."""
    if v := os.environ.get("AZURE_OPENAI_ENDPOINT"):
        cfg.endpoint = v
    if v := os.environ.get("AZURE_OPENAI_DEPLOYMENT"):
        cfg.deployment = v
    if v := os.environ.get("AZURE_OPENAI_API_VERSION"):
        cfg.api_version = v
    if v := os.environ.get("AZURE_OPENAI_MAX_TOKENS"):
        cfg.max_tokens = int(v)
    if v := os.environ.get("AZURE_OPENAI_TEMPERATURE"):
        cfg.temperature = float(v)
    return cfg


def _env_override_agent(cfg: AgentConfig) -> AgentConfig:
    """Override AgentConfig fields from environment variables."""
    if v := os.environ.get("MCPAGENT_MAX_ITERATIONS"):
        cfg.max_iterations = int(v)
    return cfg


def load_app_config(config_dir: str | Path) -> AppConfig:
    """Load config.yaml + mcp.json from *config_dir* and return AppConfig."""
    config_dir = Path(config_dir)

    # --- config.yaml ---
    yaml_path = config_dir / "config.yaml"
    if yaml_path.exists():
        with open(yaml_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    else:
        raw = {}

    app_cfg = AppConfig(**raw)

    # --- env overrides for model ---
    for name, model_cfg in app_cfg.models.items():
        _env_override_model(model_cfg)

    # --- env overrides for agent ---
    _env_override_agent(app_cfg.agent)

    # --- mcp.json ---
    mcp_path = config_dir / "mcp.json"
    app_cfg.mcp = load_mcp_config(mcp_path)

    return app_cfg
