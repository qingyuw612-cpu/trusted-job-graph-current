from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .text_utils import normalize_text, stable_id


@dataclass(frozen=True, slots=True)
class SkillDefinition:
    skill_id: str
    canonical_name: str
    aliases: tuple[str, ...]
    competency_category: str
    tech_stack: str


class SkillRegistry:
    def __init__(self, path: Path):
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.version = str(payload["version"])
        self.skills = [
            SkillDefinition(
                skill_id=stable_id("skill", item["canonical_name"]),
                canonical_name=item["canonical_name"],
                aliases=tuple(dict.fromkeys([item["canonical_name"], *item.get("aliases", [])])),
                competency_category=item["competency_category"],
                tech_stack=item["tech_stack"],
            )
            for item in payload["skills"]
        ]
        self.by_name = {normalize_text(skill.canonical_name): skill for skill in self.skills}
        for skill in self.skills:
            for alias in skill.aliases:
                self.by_name.setdefault(normalize_text(alias), skill)

    def resolve(self, value: str) -> SkillDefinition | None:
        return self.by_name.get(normalize_text(value))

    @staticmethod
    def find_alias(text: str, aliases: tuple[str, ...]) -> tuple[str, int] | None:
        for alias in sorted(aliases, key=len, reverse=True):
            escaped = re.escape(alias)
            if re.fullmatch(r"[A-Za-z0-9_.+#/-]+", alias):
                pattern = re.compile(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", re.IGNORECASE)
            else:
                pattern = re.compile(escaped, re.IGNORECASE)
            match = pattern.search(text)
            if match:
                return match.group(0), match.start()
        return None

    def as_rows(self) -> list[dict[str, str]]:
        return [
            {
                "skill_id": skill.skill_id,
                "canonical_name": skill.canonical_name,
                "aliases": "|".join(skill.aliases),
                "competency_category": skill.competency_category,
                "tech_stack": skill.tech_stack,
                "registry_version": self.version,
            }
            for skill in self.skills
        ]
