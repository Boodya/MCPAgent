"""Agent preset loader — scan .md files, parse YAML frontmatter, switch presets."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_SENTINEL = object()  # distinguish "key missing" from "key: null" in YAML


@dataclass
class AgentPreset:
    """A loaded agent preset definition."""

    name: str
    description: str = ""
    model: str = "default"
    tools: list[str] | None = None  # None = all tools; list = filter
    skills: list[str] = field(default_factory=list)
    mcp_servers: list[str] | None = None  # None = all servers; list = filter by name
    subagents: list[str] = field(default_factory=list)
    system_prompt: str = ""
    file_path: Path = field(default_factory=lambda: Path())


# Built-in default preset (used when no agents/ directory or no default.md found)
_BUILTIN_DEFAULT = AgentPreset(
    name="default",
    description="General-purpose AI assistant with full tool access",
    model="default",
    tools=None,
    skills=[],
    mcp_servers=None,
    subagents=[],
    system_prompt="",  # empty = use DEFAULT_SYSTEM_PROMPT from agent.py
)


class AgentPresetLoader:
    """Scans a directory for agent .md files and manages the active preset."""

    def __init__(self, agents_dir: str | Path) -> None:
        self.agents_dir = Path(agents_dir)
        self.presets: dict[str, AgentPreset] = {}
        self.active: AgentPreset = _BUILTIN_DEFAULT
        self._scan()
        self._ensure_default()

    def _scan(self) -> None:
        """Scan agents directory for .md files with YAML frontmatter."""
        if not self.agents_dir.is_dir():
            return

        for md_file in self.agents_dir.glob("*.md"):
            if md_file.name.upper() == "README.MD":
                continue
            preset = self._parse_preset(md_file)
            if preset:
                self.presets[preset.name] = preset

    def _ensure_default(self) -> None:
        """Ensure there is always a 'default' preset and it's active."""
        if "default" not in self.presets:
            self.presets["default"] = _BUILTIN_DEFAULT
        self.active = self.presets["default"]

    @staticmethod
    def _parse_preset(path: Path) -> AgentPreset | None:
        """Parse an agent .md file with YAML frontmatter."""
        text = path.read_text(encoding="utf-8")

        # Extract YAML frontmatter between --- markers
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
        if not match:
            return None

        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            return None

        body = text[match.end():].strip()

        name = meta.get("name", path.stem)
        # tools: None = all, list = specific tools, [] = none
        # NOT specified in YAML → defaults to [] (no tools)
        tools_val = meta.get("tools", _SENTINEL)
        if tools_val is _SENTINEL:
            tools: list[str] | None = []  # not specified → no tools
        elif tools_val == "all":
            tools = None  # explicitly "all" → all available
        elif isinstance(tools_val, list):
            tools = tools_val
        else:
            tools = []

        # mcp_servers: None = all, list = specific servers, [] = none
        # NOT specified in YAML → defaults to [] (no MCP servers)
        mcp_val = meta.get("mcp_servers", _SENTINEL)
        if mcp_val is _SENTINEL:
            mcp_servers: list[str] | None = []  # not specified → no servers
        elif mcp_val == "all":
            mcp_servers = None  # explicitly "all" → all available
        elif isinstance(mcp_val, list):
            mcp_servers = mcp_val
        else:
            mcp_servers = []

        return AgentPreset(
            name=name,
            description=meta.get("description", ""),
            model=meta.get("model", "default"),
            tools=tools,
            skills=meta.get("skills", []),
            mcp_servers=mcp_servers,
            subagents=meta.get("subagents", []),
            system_prompt=body,
            file_path=path,
        )

    def switch(self, name: str) -> AgentPreset | None:
        """Switch to a named preset. Returns the preset or None if not found."""
        preset = self.presets.get(name)
        if preset:
            self.active = preset
        return preset

    def get_names(self) -> list[str]:
        """Return sorted list of available preset names."""
        return sorted(self.presets.keys())

    def get_all(self) -> list[AgentPreset]:
        """Return all presets."""
        return list(self.presets.values())
