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
        query_words = set(re.findall(r'[a-zA-Zа-яА-ЯёЁ]+', query_lower))
        matched: list[Skill] = []

        for skill in self.skills:
            if self._skill_matches(skill, query_lower, query_words):
                matched.append(skill)

        return matched

    @staticmethod
    def _skill_matches(skill: Skill, query_lower: str, query_words: set[str]) -> bool:
        """Check if a skill matches the user query via triggers or description."""
        for trigger in skill.triggers:
            trigger_lower = trigger.lower()
            # Exact substring match (e.g. "review code" in "please review code for me")
            if trigger_lower in query_lower:
                return True
            # Word-level match: all significant trigger words present in query
            trigger_words = set(re.findall(r'[a-zA-Zа-яА-ЯёЁ]+', trigger_lower))
            significant = {w for w in trigger_words if len(w) > 2}
            if significant and significant.issubset(query_words):
                return True

        # Fallback: description keyword matching
        if skill.description:
            desc_words = set(re.findall(r'[a-zA-Zа-яА-ЯёЁ]+', skill.description.lower()))
            significant_desc = {w for w in desc_words if len(w) > 3}
            hits = len(significant_desc & query_words)
            if hits >= 2:
                return True

        return False

    def load_content(self, skill: Skill) -> str:
        """Lazily load the full content of a skill file."""
        if not skill.content:
            skill.content = skill.file_path.read_text(encoding="utf-8")
        return skill.content

    def get_all(self) -> list[Skill]:
        return list(self.skills)

    def reload(self) -> int:
        """Re-scan skills directory from disk.

        Returns the number of skills loaded.
        """
        self.skills.clear()
        self._scan()
        return len(self.skills)
