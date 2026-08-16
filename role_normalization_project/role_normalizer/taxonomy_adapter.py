"""把现有 IT 岗位分类表适配为本项目的受控岗位注册表。"""

from __future__ import annotations

import json
import sys
import csv
from dataclasses import replace
from pathlib import Path
from typing import Any

REPOSITORY_DIR = Path(__file__).resolve().parents[2]
if str(REPOSITORY_DIR) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_DIR))

from trusted_graph_agent.text_utils import stable_id

from .models import RoleDefinition
from .registry import RoleRegistry


def load_role_registry(path: str | Path) -> RoleRegistry:
    """自动识别本项目注册表或现有 it_role_taxonomy.json 格式。"""

    registry_path = Path(path)
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("岗位注册表 JSON 顶层必须是对象")

    roles = payload.get("roles")
    if roles is None and isinstance(payload.get("role_registry"), dict):
        roles = payload["role_registry"].get("roles")
    if not isinstance(roles, list):
        raise ValueError("岗位注册表缺少 roles 数组")
    if not roles:
        return RoleRegistry(())

    if "canonical_name" in roles[0]:
        return RoleRegistry.from_dict(payload)

    converted: list[RoleDefinition] = []
    for raw in roles:
        if not isinstance(raw, dict):
            raise ValueError("roles 中的每一项必须是对象")
        name = str(raw.get("role_name") or "").strip()
        if not name:
            raise ValueError("现有分类表包含空 role_name")
        converted.append(
            RoleDefinition(
                role_id=stable_id("role", name),
                canonical_name=name,
                aliases=[str(item).strip() for item in raw.get("aliases", []) if str(item).strip()],
                family=str(raw.get("family_id") or ""),
                description=str(raw.get("description") or ""),
                tags=[str(item).strip() for item in raw.get("tags", []) if str(item).strip()],
                metadata={"parent_role": raw.get("parent_role", ""), "source": str(registry_path)},
            )
        )
    return RoleRegistry(converted)


def registry_summary(registry: RoleRegistry) -> dict[str, Any]:
    """生成不包含向量的轻量注册表摘要。"""

    return {
        "role_count": len(registry),
        "families": sorted({role.family for role in registry if role.family}),
    }


def enrich_registry_with_role_skills(
    registry: RoleRegistry,
    path: str | Path,
    *,
    categories: tuple[str, ...] = ("技术", "知识"),
    max_skills_per_role: int = 20,
) -> RoleRegistry:
    """从现有岗位核心技能报告补充岗位画像，不修改原注册表对象。"""

    report_path = Path(path)
    if not report_path.is_file():
        return registry
    grouped: dict[str, list[tuple[float, str]]] = {}
    with report_path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if str(row.get("category") or "") not in categories:
                continue
            role_name = str(row.get("role") or "").strip()
            skill_name = str(row.get("canonical_name") or "").strip()
            if not role_name or not skill_name:
                continue
            try:
                score = float(row.get("final_score") or 0.0)
            except ValueError:
                score = 0.0
            grouped.setdefault(role_name, []).append((score, skill_name))

    enriched: list[RoleDefinition] = []
    for role in registry:
        ranked = sorted(grouped.get(role.canonical_name, []), key=lambda item: (-item[0], item[1]))
        skills = list(dict.fromkeys(name for _score, name in ranked))[:max_skills_per_role]
        metadata = dict(role.metadata)
        if skills:
            metadata["skills"] = skills
            metadata["skills_source"] = str(report_path)
        enriched.append(replace(role, metadata=metadata))
    return RoleRegistry(enriched)
