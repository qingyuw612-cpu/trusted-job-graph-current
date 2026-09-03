"""Role 排名 + 加权覆盖率计算 — 纯函数，零外部依赖。

职责：
- 简历原文 vs 全部 Role 的核心技能加权覆盖率粗排（final_score 支持度加权）
- 少条目惩罚（核心技能 < K 的 Role 按 n/K 打折）
- IDF 重加权（可选开关，默认关闭，供消融对比）
- 七维命中明细
"""

import copy
import math
from typing import Any, Dict, List, Optional, Sequence

from .dimensions import CATEGORY_TO_DIM
from .matching import match_skills_in_text

# 少技能惩罚阈值：核心技能数少于该值的 Role，覆盖率按 n/K 打折
CORE_SKILL_PENALTY_K = 10


def _apply_idf(roles: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按技能的跨 Role 稀有度重新加权（IDF 思想）。

    weight' = weight × log(N / df)
    - N: 有核心技能的 Role 总数
    - df: 该技能出现在多少个 Role 中（document frequency）
    通用技能 df 大 → 权重趋近 0；职业特有技能 df 小 → 权重保留。

    Args:
        roles: 原始 Role 列表（不改动原数据）。

    Returns:
        深拷贝并重新加权后的 Role 列表。
    """
    weighted = copy.deepcopy(list(roles))

    # 统计每个技能出现的 Role 数（df）
    df: Dict[str, int] = {}
    for role in weighted:
        for skill in role.get("skills", []):
            name = skill.get("name", "")
            if name:
                df[name] = df.get(name, 0) + 1

    n_roles = sum(1 for r in weighted if r.get("skills"))
    if n_roles <= 1:
        return weighted

    for role in weighted:
        for skill in role.get("skills", []):
            name = skill.get("name", "")
            if not name:
                continue
            doc_freq = max(1, df.get(name, 1))
            idf = math.log(n_roles / doc_freq)
            skill["weight"] = skill["weight"] * max(idf, 0.0)

    return weighted


def rank_roles(
    raw_text: str,
    roles: Sequence[Dict[str, Any]],
    topk: Optional[int] = None,
    use_idf: bool = False,
) -> List[Dict[str, Any]]:
    """简历原文 vs 全部 Role 的核心技能加权覆盖率粗排。

    直接在简历原文（Markdown）中做归一化子串搜索，
    不依赖任何提取结果。

    评分公式：
        score = (Σ命中技能 final_score / Σ全部技能 final_score) × min(1, 技能总数/10)
    final_score 取图谱 HAS_CORE_SKILL 边权重（JD 支持度），全部权重为 0 时
    回退为纯命中率，避免除零。use_idf=True 时先按技能跨岗位稀有度重加权
    （消融对比用，默认关闭）。

    Args:
        raw_text: 简历 Markdown 原文。
        roles: store 层加载的 Role 列表（role_name/skills/jd_count 等）。
        topk: 只返回前 N 名（None/<=0 返回全部）。
        use_idf: 是否启用跨岗位 IDF 重加权（默认 False）。

    Returns:
        按 score 降序的列表，每条：
        {"role_name", "family_name", "domain_name", "jd_count",
         "score", "hit_skills", "total_skills"}
    """
    if use_idf:
        roles = _apply_idf(roles)

    scored: List[Dict[str, Any]] = []
    for role in roles:
        if role.get("jd_count", 0) <= 0:
            continue
        skills = [s for s in role.get("skills", []) if s.get("name")]
        if not skills:
            continue

        result = match_skills_in_text(raw_text, skills)

        n_skills = len(skills)
        # 与 match 的维度口径一致：只统计有有效 category 映射的技能
        dim_valid = [s for s in skills if s.get("category") in CATEGORY_TO_DIM]
        total_weight = sum(float(s.get("weight") or 0.0) for s in dim_valid)
        hit_weight = sum(float(e.get("weight") or 0.0) for e in result["hit"])
        if total_weight > 0:
            coverage = hit_weight / total_weight
        else:
            # 全部权重为 0（如数据缺失）时回退为纯命中率
            coverage = result["hit_count"] / max(len(dim_valid), 1)
        penalty = min(1.0, n_skills / CORE_SKILL_PENALTY_K)
        score = round(coverage * penalty, 4)

        scored.append(
            {
                "role_name": role.get("role_name", ""),
                "family_name": role.get("family_name", ""),
                "domain_name": role.get("domain_name", ""),
                "jd_count": role.get("jd_count", 0),
                "score": score,
                "hit_skills": result["hit_count"],
                "total_skills": n_skills,
            }
        )

    scored.sort(key=lambda x: x["score"], reverse=True)
    if topk and topk > 0:
        scored = scored[:topk]
    return scored


def compute_dimension_hits(
    raw_text: str,
    role_skills: Sequence[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """按七维统计简历原文对 Role 技能的命中/未命中明细。

    Args:
        raw_text: 简历 Markdown 原文。
        role_skills: 单个 Role 的技能列表 [{name, category, weight, rank}, ...]。

    Returns:
        {"knowledge": {"hit": [...], "miss": [...], "coverage": 0.67,
                       "total": 3, "hit_count": 2, "miss_count": 1}, ...}
    """
    full = match_skills_in_text(raw_text, role_skills)
    result: Dict[str, Dict[str, Any]] = {}
    for dim, bd in full.get("by_dim", {}).items():
        names_hit = [e["name"] for e in bd.get("hit", [])]
        names_miss = [e["name"] for e in bd.get("miss", [])]
        total = bd["total"]
        result[dim] = {
            "hit": names_hit,
            "miss": names_miss,
            "coverage": round(bd["hit_count"] / max(total, 1), 4),
            "total": total,
            "hit_count": bd["hit_count"],
            "miss_count": len(names_miss),
        }
    return result

