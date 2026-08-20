"""rank_resume 工具 — 简历原文 vs 全部 Role 的纯关键词命中排名。"""

import os
from typing import Any, Dict, List, Optional

from ..core.ranking import rank_roles, compute_dimension_hits
from ..store import RoleStore, create_store


def _get_store() -> RoleStore:
    """按 STORE_BACKEND 环境变量选择数据源，默认 memory。"""
    backend = os.getenv("STORE_BACKEND", "memory")
    return create_store(backend)


def rank_resume(
    resume_text: str,
    topk: int = 10,
    store: Optional[RoleStore] = None,
    use_idf: bool = False,
) -> Dict[str, Any]:
    """对简历原文做 Role 粗排，返回 Top-N 及七维覆盖率。

    Args:
        resume_text: 简历 Markdown 原文（非空）。
        topk: 返回前 N 名，默认 10。
        store: 可选 RoleStore 实例；缺省按 STORE_BACKEND 自动选择。
        use_idf: 是否启用跨岗位 IDF 重加权（默认 False；消融对比用）。

    Returns:
        {
            "topk": int,
            "count": int,
            "results": [
                {
                    "role_name": str,
                    "family_name": str,
                    "domain_name": str,
                    "score": float,           # 加权覆盖率 × 少条目惩罚
                    "hit_skills": int,
                    "total_skills": int,
                    "skill_weights": {str: float},  # 技能名 → final_score（复核合并重算加权分用）
                    "dimensions": {dim: {"hit": [...], "miss": [...], "coverage": float,
                                         "total": int, "hit_count": int, "miss_count": int}},
                },
                ...
            ],
        }
    """
    text = (resume_text or "").strip()
    if not text:
        raise ValueError("resume_text 不能为空，请先提供简历 Markdown 原文。")

    store = store or _get_store()
    roles = store.get_all_roles()
    if not roles:
        raise RuntimeError("Role 数据为空，请检查 STORE_BACKEND 配置。")

    ranked = rank_roles(
        text,
        roles,
        topk=topk if topk and topk > 0 else None,
        use_idf=use_idf,
    )
    results: List[Dict[str, Any]] = []
    for item in ranked:
        role = store.get_role_by_name(item["role_name"]) or {}
        skills = [s for s in role.get("skills", []) if s.get("name")]
        results.append(
            {
                "role_name": item["role_name"],
                "family_name": item["family_name"],
                "domain_name": item["domain_name"],
                "score": item["score"],
                "hit_skills": item["hit_skills"],
                "total_skills": item["total_skills"],
                "skill_weights": {
                    s.get("name", ""): float(s.get("weight") or 0.0)
                    for s in skills
                    if s.get("name")
                },
                "dimensions": compute_dimension_hits(text, skills),
            }
        )

    return {
        "topk": len(results),
        "count": len(ranked),
        "results": results,
    }

