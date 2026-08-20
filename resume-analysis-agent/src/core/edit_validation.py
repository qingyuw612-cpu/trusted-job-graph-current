"""简历修改建议防造假校验 — 纯逻辑，零外部依赖。

校验维度：
1. 结构：suggestions 数组、status 枚举、必填字段
2. 技能地基：建议中的技能必须属于岗位核心技能清单（hit/miss 全集）
3. 状态一致性：hit 必须真实命中；missing 必须确实缺失
4. 指标地基：建议/改写示例中的量化指标必须能在原简历中找到依据
5. 新增技能风险：缺失技能被建议写入且简历无依据 → 高风险
6. AI 味词汇：命中黑名单提示替换
"""

import re
from typing import Any, Dict, List

from ..prompts.modify import MODIFY_AI_PHRASES

_VALID_STATUS = {"hit", "reinforce", "missing"}

_METRIC_PATTERNS = [
    re.compile(r"\d+(?:\.\d+)?\s*[%％倍xXwW]"),
    re.compile(r"\d+(?:\.\d+)?\s*(?:ms|秒|分钟|小时|天|周|月|QPS|TPS|RPS|并发|万|亿|人|次|单|个|GB|MB|TB|G|M|K)"),
    re.compile(r"\d{3,}"),
]

_NEGATIVE_DIRECTIVE = ("不建议", "不要", "避免", "否则不加", "不硬写", "除非", "无需")
_ADD_INTENT = ("补充", "添加", "写入", "增加", "补上", "加上")


def _norm(text: str) -> str:
    """轻量归一化：去空白/常见标点/全角转半角/小写（与 matching._normalize 口径一致）。"""
    t = text or ""
    t = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in t)
    t = re.sub(r"[\s，。；、,.!?！？:：;；()（）\[\]【】/\\|~`·\-—_]+", "", t)
    return t.lower()


def extract_metrics(text: str) -> List[str]:
    """提取文本中的量化指标 token（去重、保序）。"""
    if not text:
        return []
    found: List[str] = []
    seen = set()
    for pattern in _METRIC_PATTERNS:
        for m in pattern.finditer(text):
            token = m.group(0).strip()
            if token and token not in seen:
                seen.add(token)
                found.append(token)
    return found


def _skill_in_text(skill: str, text: str) -> bool:
    norm_skill = _norm(skill)
    if not norm_skill:
        return False
    return norm_skill in _norm(text)


def _role_skill_universe(role: Dict[str, Any]) -> Dict[str, set]:
    """从 role.dimensions 汇总 hit/miss 技能全集。"""
    hit: set = set()
    miss: set = set()
    for detail in (role.get("dimensions") or {}).values():
        hit.update(str(x) for x in (detail.get("hit") or []))
        miss.update(str(x) for x in (detail.get("miss") or []))
    return {"hit": hit, "miss": miss, "universe": hit | miss}


def validate_edit_suggestions(
    role: Dict[str, Any],
    resume_text: str,
    edit_json: Dict[str, Any],
) -> Dict[str, Any]:
    """校验 Agent 生成的简历修改建议，返回风险报告。

    Args:
        role: 单个 role JSON（含 dimensions hit/miss）。
        resume_text: 简历 Markdown 原文（用于指标/技能地基校验）。
        edit_json: prepare_resume_edit 输出 schema 对应的建议 JSON。

    Returns:
        {"role_name", "valid", "summary", "violations",
         "stats", "checklist"}
    """
    role_name = role.get("role_name", "")
    violations: List[Dict[str, Any]] = []
    suggestions = edit_json.get("suggestions")
    stats = {
        "suggestions": 0,
        "hit": 0,
        "reinforce": 0,
        "missing": 0,
        "unknown_skills": 0,
        "fabricated_metrics": 0,
        "ai_phrases": 0,
    }
    checklist = {
        "structure": True,
        "skill_grounding": True,
        "status_consistency": True,
        "metric_grounding": True,
        "ai_phrase_check": True,
    }

    # ---- 1. 结构校验 ----
    if not isinstance(suggestions, list):
        return {
            "role_name": role_name,
            "valid": False,
            "summary": "结构无效：suggestions 必须是数组",
            "violations": [
                {
                    "level": "critical",
                    "item": "edit_json",
                    "field": "suggestions",
                    "message": "suggestions 必须是数组",
                }
            ],
            "stats": stats,
            "checklist": {**checklist, "structure": False},
        }
    if not suggestions:
        return {
            "role_name": role_name,
            "valid": True,
            "summary": "没有建议可校验",
            "violations": [],
            "stats": stats,
            "checklist": checklist,
        }

    stats["suggestions"] = len(suggestions)
    universe = _role_skill_universe(role)
    for i, item in enumerate(suggestions):
        label = f"suggestions[{i}]"
        if not isinstance(item, dict):
            checklist["structure"] = False
            violations.append(
                {"level": "critical", "item": label, "field": "-",
                 "message": "建议项必须是对象"}
            )
            continue

        skill = str(item.get("skill") or "").strip()
        status = str(item.get("status") or "").strip()
        suggestion = str(item.get("suggestion") or "").strip()
        rewrite = str(item.get("example_rewrite") or "").strip()

        if not skill:
            checklist["structure"] = False
            violations.append(
                {"level": "critical", "item": label, "field": "skill",
                 "message": "缺少技能名"}
            )
        if status not in _VALID_STATUS:
            checklist["structure"] = False
            violations.append(
                {"level": "critical", "item": label, "field": "status",
                 "message": f"status 必须是 {sorted(_VALID_STATUS)} 之一，实际为 {status!r}"}
            )
        if not suggestion and not rewrite:
            checklist["structure"] = False
            violations.append(
                {"level": "warning", "item": label, "field": "suggestion",
                 "message": "suggestion 与 example_rewrite 均为空，建议无内容"}
            )
        if status in stats:
            stats[status] += 1

        # ---- 2. 技能地基 ----
        if skill:
            if skill not in universe["universe"]:
                checklist["skill_grounding"] = False
                stats["unknown_skills"] += 1
                violations.append(
                    {"level": "warning", "item": label, "field": "skill",
                     "message": f"技能“{skill}”不在岗位核心技能清单中"}
                )

            # ---- 3. 状态一致性 ----
            if status == "hit" and skill not in universe["hit"]:
                checklist["status_consistency"] = False
                violations.append(
                    {"level": "warning", "item": label, "field": "status",
                     "message": f"标记为已命中，但“{skill}”不在复核命中清单中"}
                )
            if status == "missing" and skill in universe["hit"]:
                checklist["status_consistency"] = False
                violations.append(
                    {"level": "info", "item": label, "field": "status",
                     "message": f"标记为缺失，但“{skill}”实际在命中清单中"}
                )
            if status == "reinforce" and not _skill_in_text(skill, resume_text):
                checklist["status_consistency"] = False
                violations.append(
                    {"level": "warning", "item": label, "field": "status",
                     "message": f"标记为可强化，但简历原文未体现“{skill}”"}
                )

            # ---- 5. 新增技能风险（识别负向/条件表述，避免误报） ----
            if status == "missing" and skill in universe["miss"]:
                combined_text = f"{suggestion} {rewrite}"
                has_negative = any(d in combined_text for d in _NEGATIVE_DIRECTIVE)
                has_add_intent = skill in rewrite or any(d in combined_text for d in _ADD_INTENT)
                if (
                    not _skill_in_text(skill, resume_text)
                    and has_add_intent
                    and not has_negative
                ):
                    checklist["skill_grounding"] = False
                    violations.append(
                        {"level": "critical", "item": label, "field": "example_rewrite",
                         "message": f"缺失技能“{skill}”被建议写入，但简历中没有任何依据，存在造假风险"}
                    )
                elif (
                    not _skill_in_text(skill, resume_text)
                    and skill in combined_text
                    and has_negative
                ):
                    violations.append(
                        {"level": "info", "item": label, "field": "suggestion",
                         "message": f"技能“{skill}”提及但明确不建议写入，已识别为负向表述"}
                    )
            if status == "hit" and not _skill_in_text(skill, resume_text):
                violations.append(
                    {"level": "warning", "item": label, "field": "status",
                     "message": f"标记为已命中，但简历原文未直接出现“{skill}”，请人工确认（可能是语义命中）"}
                )

        # ---- 4. 指标地基 ----
        for field_name, field_text in (("suggestion", suggestion), ("example_rewrite", rewrite)):
            if not field_text:
                continue
            for metric in extract_metrics(field_text):
                if _norm(metric) not in _norm(resume_text):
                    checklist["metric_grounding"] = False
                    stats["fabricated_metrics"] += 1
                    violations.append(
                        {"level": "warning", "item": label, "field": field_name,
                         "message": f"出现量化指标“{metric}”，但原简历中未找到依据"}
                    )

        # ---- 6. AI 味词汇 ----
        combined = f"{suggestion} {rewrite}"
        for phrase in MODIFY_AI_PHRASES:
            if phrase in combined:
                checklist["ai_phrase_check"] = False
                stats["ai_phrases"] += 1
                violations.append(
                    {"level": "info", "item": label, "field": "-",
                     "message": f"命中 AI 味词汇“{phrase}”，建议替换为“{MODIFY_AI_PHRASES[phrase]}”"}
                )

    critical_count = sum(1 for v in violations if v["level"] == "critical")
    warning_count = sum(1 for v in violations if v["level"] == "warning")
    info_count = sum(1 for v in violations if v["level"] == "info")
    valid = critical_count == 0
    if critical_count:
        summary = f"发现 {critical_count} 个高风险问题（{warning_count} 个警告，{info_count} 个提示），建议修正后使用"
    elif warning_count:
        summary = f"无高风险问题，但有 {warning_count} 个警告（{info_count} 个提示），建议人工确认"
    else:
        summary = f"校验通过：{stats['suggestions']} 条建议全部安全"

    return {
        "role_name": role_name,
        "valid": valid,
        "summary": summary,
        "violations": violations,
        "stats": stats,
        "checklist": checklist,
    }

