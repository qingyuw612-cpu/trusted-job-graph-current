from __future__ import annotations

import json
from pathlib import Path

from .text_utils import normalize_text, stable_id


class RoleTaxonomy:
    def __init__(self, path: Path):
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.version = str(payload["version"])
        self.domain = dict(payload["domain"])
        self.families = [dict(item) for item in payload["families"]]
        self.family_by_id = {item["family_id"]: item for item in self.families}
        self.roles = [dict(item) for item in payload["roles"]]
        self.role_by_name = {item["role_name"]: item for item in self.roles}
        self.role_by_normalized_name: dict[str, dict] = {}
        title_aliases: list[tuple[str, dict]] = []
        self.role_by_source: dict[str, dict] = {}
        for role in self.roles:
            for value in [role["role_name"], *role.get("aliases", [])]:
                normalized = normalize_text(str(value))
                if not normalized:
                    continue
                self.role_by_normalized_name[normalized] = role
                title_aliases.append((normalized, role))
            for source in role.get("sources", []):
                self.role_by_source[source.casefold()] = role
        self.title_aliases = sorted(
            title_aliases,
            key=lambda item: len(item[0]),
            reverse=True,
        )

    def resolve_source(self, relative_path: str) -> dict | None:
        return self.role_by_source.get(relative_path.replace("\\", "/").casefold())

    def role(self, role_name: str) -> dict | None:
        return self.role_by_name.get(role_name)

    def resolve_title(self, title: str, declared_role: str = "") -> dict | None:
        """Resolve an actual vacancy title to the controlled IT role taxonomy."""
        normalized_title = normalize_text(title)
        if normalized_title:
            exact = self.role_by_normalized_name.get(normalized_title)
            if exact:
                return exact
            for alias, role in self.title_aliases:
                if len(alias) >= 4 and alias in normalized_title:
                    return role
        return self.role_by_normalized_name.get(normalize_text(declared_role))

    def family_rows(self) -> list[dict]:
        return [
            {
                "family_id": item["family_id"],
                "family_name": item["family_name"],
                "domain_id": self.domain["domain_id"],
                "domain_name": self.domain["domain_name"],
            }
            for item in self.families
        ]

    def alias_rows(self) -> list[dict]:
        rows = []
        for role in self.roles:
            for alias in [role["role_name"], *role.get("aliases", [])]:
                rows.append(
                    {
                        "alias_id": stable_id("role_alias", role["role_name"], alias),
                        "role_id": stable_id("role", role["role_name"]),
                        "role_name": role["role_name"],
                        "alias": alias,
                    }
                )
        return rows

    def relation_rows(self) -> list[dict]:
        rows = []
        for role in self.roles:
            parent = role.get("parent_role", "")
            if not parent:
                continue
            rows.append(
                {
                    "relation_id": stable_id("role_relation", parent, role["role_name"]),
                    "parent_role_id": stable_id("role", parent),
                    "child_role_id": stable_id("role", role["role_name"]),
                    "relation": "HAS_SUBTYPE",
                }
            )
        return rows
