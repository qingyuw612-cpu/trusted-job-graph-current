"""analyze_gap 工具 — 基于结构化命中清单的 LLM 差距分析 + 学习路径。"""

import json
from typing import Any, Dict, List, Optional

from ..core.dimensions import DIM_LABELS
from ..prompts.gap_analysis import ROLE_GAP_PROMPT
from ..utils.llm import call_llm_json


GAP_DIM_ORDER = (
    "knowledge",
    "skill",
    "qualifications",
    "preference",
    "motivation",
    "trait",
    "self_concept",
)

_IMPORTANCE_RANK = {"high": 0, "medium": 1, "low": 2}


def _build_dimension_details(role: Dict[str, Any]) -> str:
    """把单 Role 的七维命中明细格式化为 prompt 输入块。"""
    dims = role.get("dimensions") or {}
    lines = []
    for dim in ("knowledge", "skill", "qualifications", "preference",
                "motivation", "trait", "self_concept"):
        detail = dims.get(dim) or {}
        label = DIM_LABELS.get(dim, dim)
        hit = detail.get("hit", [])
        miss = detail.get("miss", [])
        lines.append(f"- {label} ({dim}): 命中={hit or '无'} | 缺失={miss or '无'}")
    return "\n".join(lines)


def weighted_missing_skills(role: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从 role 维度 miss 明细提取缺失技能，附图谱权重并按权重降序（纯逻辑）。

    权重取 role.skill_weights（HAS_CORE_SKILL.final_score），查不到按 0.0。
    返回: [{"skill", "dim", "weight"}, ...]（weight 降序）
    """
    dims = role.get("dimensions") or {}
    weights = role.get("skill_weights") or {}
    items: List[Dict[str, Any]] = []
    for dim in GAP_DIM_ORDER:
        detail = dims.get(dim) or {}
        for name in detail.get("miss", []) or []:
            name = str(name)
            items.append(
                {
                    "skill": name,
                    "dim": dim,
                    "weight": round(float(weights.get(name, 0.0) or 0.0), 4),
                }
            )
    items.sort(key=lambda x: x["weight"], reverse=True)
    return items


def _build_missing_block(role: Dict[str, Any]) -> str:
    """把权重降序的缺失技能列表格式化为 prompt 输入块。"""
    items = weighted_missing_skills(role)
    if not items:
        return "（无缺失技能）"
    return "\n".join(
        f"- {m['skill']} [{m['dim']}] (w={m['weight']:.4f})" for m in items
    )


def build_gap_report(
    role: Dict[str, Any],
    llm_analysis: Dict[str, Any],
) -> Dict[str, Any]:
    """把 role 命中明细 + LLM 差距分析合并为结构化 gap 报告（B2 契约）。

    报告包含：匹配结论、7 维得分（0-1 覆盖率 + 命中/缺失）、缺失技能清单
    （按图谱权重降序）、学习路径（B3 字段，LLM 综合排序）、整体建议。
    """
    dims = role.get("dimensions") or {}
    llm_dims = llm_analysis.get("dimensions") or {}
    match = llm_analysis.get("match") or {}

    report_dims: Dict[str, Dict[str, Any]] = {}
    for dim in GAP_DIM_ORDER:
        detail = dims.get(dim) or {}
        ld = llm_dims.get(dim) or {}
        total = detail.get("total", 0)
        coverage = float(detail.get("coverage", 0.0) or 0.0)
        if not total:
            fallback_level = "sufficient"
        elif coverage <= 0:
            fallback_level = "missing"
        elif coverage < 1:
            fallback_level = "partial"
        else:
            fallback_level = "sufficient"
        report_dims[dim] = {
            "score": round(coverage, 4),
            "gap_level": ld.get("gap_level") or fallback_level,
            "summary": ld.get("summary", ""),
            "hit": detail.get("hit", []),
            "missing": detail.get("miss", []),
        }

    # 缺失技能清单：以 role 维度 miss 为准，按图谱权重降序；
    # LLM 的 importance 仅作为标注（按技能名合并），查不到回退 medium。
    raw_missing = llm_analysis.get("missing_skills")
    llm_importance: Dict[str, str] = {}
    if isinstance(raw_missing, list):
        for m in raw_missing:
            if m.get("skill"):
                llm_importance[str(m["skill"])] = str(m.get("importance", "medium"))

    weighted = weighted_missing_skills(role)
    if weighted:
        missing_skills = [
            {
                "skill": m["skill"],
                "dim": m["dim"],
                "importance": llm_importance.get(m["skill"], "medium"),
                "weight": m["weight"],
            }
            for m in weighted
        ]
    else:
        # 极端情况：role 没有维度 miss 明细，回退 LLM 列表（按 importance 排序）
        missing_skills = [
            {
                "skill": str(m.get("skill", "")),
                "dim": str(m.get("dim", "")),
                "importance": str(m.get("importance", "medium")),
                "weight": 0.0,
            }
            for m in (raw_missing or [])
            if m.get("skill")
        ]
        missing_skills.sort(
            key=lambda x: _IMPORTANCE_RANK.get(x.get("importance", "medium"), 1)
        )

    # 学习路径：按 B3 契约规范化（LLM 缺字段时给空值/空数组，前端可直接渲染）
    learning_path: List[Dict[str, Any]] = []
    for i, step in enumerate(llm_analysis.get("learning_path") or [], start=1):
        resources = step.get("resources") if isinstance(step.get("resources"), list) else []
        learning_path.append(
            {
                "step": int(step.get("step") or i),
                "skill": str(step.get("skill", "")),
                "importance": str(step.get("importance", "medium")),
                "prerequisite": str(step.get("prerequisite") or "无"),
                "resources": [str(r) for r in resources],
                "estimated_effort": str(step.get("estimated_effort") or ""),
                "why": str(step.get("why") or ""),
            }
        )

    return {
        "role_name": role.get("role_name", ""),
        "match": {
            "verdict": match.get("verdict", "no"),
            "reason": match.get("reason", ""),
        },
        "dimensions": report_dims,
        "missing_skills": missing_skills,
        "learning_path": learning_path,
        "overall_advice": llm_analysis.get("overall_summary", ""),
    }


def _format_markdown(role: Dict[str, Any], llm_result: Dict[str, Any]) -> str:
    """把 LLM JSON 结果格式化为可读 Markdown。"""
    name = role.get("role_name", "目标岗位")
    match = llm_result.get("match") or {}
    verdict = "匹配" if match.get("verdict") == "yes" else "不匹配"
    md = [
        f"## {name} — 差距分析",
        "",
        f"**匹配结论**: {verdict}  ·  {match.get('reason', '')}",
        "",
    ]
    dims = llm_result.get("dimensions") or {}
    for dim, label in DIM_LABELS.items():
        detail = dims.get(dim) or {}
        if not detail:
            continue
        md.append(f"### {label}")
        md.append(detail.get("summary", ""))
        md.append("")
    if llm_result.get("overall_summary"):
        md.append("### 总体建议")
        md.append(llm_result["overall_summary"])
        md.append("")
    learning_path = llm_result.get("learning_path") or []
    if learning_path:
        md.append("### 学习路径")
        for step in learning_path:
            md.append(f"{step.get('step', '?')}. **{step.get('skill', '')}**"
                      f"（{step.get('importance', '')}）")
        md.append("")
    return "\n".join(md)


def prepare_gap(role: Dict[str, Any], resume_text: str) -> Dict[str, Any]:
    """Agent mode: build the gap-analysis payload without calling any LLM API.

    The agent uses its own model to write the gap analysis + learning path,
    and returns a Markdown report directly (no server-side normalization).
    """
    text = (resume_text or "").strip()
    if not role or not role.get("role_name"):
        raise ValueError("role invalid: missing role_name")
    prompt = ROLE_GAP_PROMPT.format(
        role_name=role.get("role_name", ""),
        family_name=role.get("family_name", ""),
        domain_name=role.get("domain_name", ""),
        dimension_details=_build_dimension_details(role),
        missing_sorted=_build_missing_block(role),
        resume_raw_text=text[:12000],
    )
    return {
        "mode": "agent_analysis",
        "purpose": "Write a gap analysis and learning path with your own model",
        "prompt": prompt,
        "role_name": role.get("role_name", ""),
        "dimension_details": _build_dimension_details(role),
        "resume_text": text[:12000],
        "output_format": (
            "Markdown report with: match verdict, per-dimension analysis, "
            "overall advice, numbered learning path"
        ),
    }


def analyze_gap(
    role: Dict[str, Any],
    resume_text: str,
    llm_func: Optional[Any] = None,
) -> Dict[str, Any]:
    """生成单个 Role 的差距分析与学习路径。

    Args:
        role: 单个 Role 的完整 JSON（含 dimensions 命中明细），
              来自 rank_resume() 的 results[i]。
        resume_text: 简历 Markdown 原文。
        llm_func: 可注入的 LLM 调用函数（测试用）。

    Returns:
        {
            "role_name": str,
            "analysis": {...},   # LLM 原始 JSON（match/dimensions/learning_path...）
            "markdown": str,     # 格式化后的 Markdown
        }
    """
    text = (resume_text or "").strip()
    if not text:
        raise ValueError("resume_text 不能为空。")
    if not role or not role.get("role_name"):
        raise ValueError("role 无效：缺少 role_name。")

    prompt = ROLE_GAP_PROMPT.format(
        role_name=role.get("role_name", ""),
        family_name=role.get("family_name", ""),
        domain_name=role.get("domain_name", ""),
        dimension_details=_build_dimension_details(role),
        missing_sorted=_build_missing_block(role),
        resume_raw_text=text[:12000],
    )
    caller = llm_func or call_llm_json
    llm_result = caller(prompt)
    if not isinstance(llm_result, dict):
        raise RuntimeError("LLM 差距分析返回格式异常：期望 JSON 对象。")

    return {
        "role_name": role.get("role_name", ""),
        "analysis": llm_result,
        "markdown": _format_markdown(role, llm_result),
        "report": build_gap_report(role, llm_result),
    }

