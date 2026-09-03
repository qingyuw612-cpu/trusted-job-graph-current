from __future__ import annotations

import csv
import json
from pathlib import Path

from trusted_graph_agent.text_utils import normalize_text


ROOT = Path(__file__).resolve().parents[1]


def _taxonomy_names() -> tuple[dict[str, str], set[str]]:
    payload = json.loads(
        (ROOT / "trusted_graph_agent" / "it_role_taxonomy.json").read_text(
            encoding="utf-8"
        )
    )
    family_ids = {row["family_id"] for row in payload["families"]}
    names: dict[str, str] = {}
    for role in payload["roles"]:
        assert role["family_id"] in family_ids
        for value in [role["role_name"], *role.get("aliases", [])]:
            key = normalize_text(value)
            assert key
            assert key not in names or names[key] == role["role_name"]
            names[key] = role["role_name"]
    return names, family_ids


def test_taxonomy_covers_all_checked_in_role_classifications() -> None:
    names, family_ids = _taxonomy_names()
    with (
        ROOT / "role_normalization_project" / "role_family_classification.csv"
    ).open("r", encoding="utf-8-sig", newline="") as file:
        csv_roles = {normalize_text(row["role_name"]) for row in csv.DictReader(file)}

    assert csv_roles <= names.keys()
    assert "extended_roles" not in family_ids


def test_previously_unclassified_online_roles_have_explicit_families() -> None:
    names, _ = _taxonomy_names()
    previously_unclassified = {
        "现场应用工程师（FAE）", "软件工程师", "系统集成工程师", "网络安全工程师",
        "AI应用工程师", "全栈开发工程师", "数据治理工程师", "射频工程师",
        "数据工程师", "IT技术支持工程师", "IT项目经理", "IT经理", "PHP开发工程师",
        "软件实施工程师", "MES工程师", "数据挖掘工程师", "ERP实施工程师",
        "售前解决方案工程师", "量化开发工程师", "解决方案工程师", "数据科学家",
        "雷达系统工程师", "企业应用开发工程师", "5G核心网研发工程师",
        "具身智能系统工程师", "游戏策划", "编译器开发工程师", "AI部署工程师",
        "UI设计师", "RPA工程师",
    }

    assert {normalize_text(role) for role in previously_unclassified} <= names.keys()
