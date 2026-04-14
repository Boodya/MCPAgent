"""Skill loader — scan SKILL.md files, match to user queries, inject into prompt."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Skill:
    """A loaded skill definition."""

    name: str
    description: str
    triggers: list[str] = field(default_factory=list)
    file_path: Path = field(default_factory=lambda: Path())
    content: str = ""  # lazily loaded


class SkillLoader:
    """Scans a directory for SKILL.md files and provides matching logic."""

    def __init__(self, skills_dir: str | Path) -> None:
        self.skills_dir = Path(skills_dir)
        self.skills: list[Skill] = []
        self._scan()

    def _scan(self) -> None:
        """Scan skills directory for SKILL.md files with YAML frontmatter."""
        if not self.skills_dir.is_dir():
            return

        for md_file in self.skills_dir.rglob("SKILL.md"):
            skill = self._parse_skill(md_file)
            if skill:
                self.skills.append(skill)

    @staticmethod
    def _parse_skill(path: Path) -> Skill | None:
        """Parse a SKILL.md file with YAML frontmatter."""
        text = path.read_text(encoding="utf-8")

        # Extract YAML frontmatter between --- markers
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
        if not match:
            # No frontmatter — use filename as name
            return Skill(
                name=path.parent.name or path.stem,
                description="",
                file_path=path,
            )

        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            meta = {}

        body = text[match.end():]

        return Skill(
            name=meta.get("name", path.parent.name or path.stem),
            description=meta.get("description", ""),
            triggers=meta.get("triggers", []),
            file_path=path,
            content=body,
        )

    def match(self, user_query: str) -> list[Skill]:
        """Return skills whose triggers or description match the user query."""
        query_lower = user_query.lower()
        matched: list[Skill] = []

        for skill in self.skills:
            # Check triggers
            for trigger in skill.triggers:
                if trigger.lower() in query_lower:
                    matched.append(skill)
                    break
            else:
                # Check description keywords
                if skill.description:
                    desc_words = skill.description.lower().split()
                    # Match if a significant portion of description words appear in query
                    hits = sum(1 for w in desc_words if len(w) > 3 and w in query_lower)
                    if hits >= 2:
                        matched.append(skill)

        return matched

    def load_content(self, skill: Skill) -> str:
        """Lazily load the full content of a skill file."""
        if not skill.content:
            skill.content = skill.file_path.read_text(encoding="utf-8")
        return skill.content

    def get_all(self) -> list[Skill]:
        return list(self.skills)
