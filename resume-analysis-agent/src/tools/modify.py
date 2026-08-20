"""简历修改建议工具 — 针对目标岗位的定向修改（不重写全文）。

双模式：
- MCP/Agent 模式：prepare_resume_edit 返回提示包，Agent 用自己的模型产出建议；
- CLI 模式：suggest_resume_edit 直接调用当前配置的 LLM（默认 DeepSeek，可切讯飞等）。
"""

import json
import re
from typing import Any, Dict, List, Optional

from ..core.edit_validation import validate_edit_suggestions
from ..prompts.modify import MODIFY_AI_PHRASES, MODIFY_PROMPT
from ..utils.llm import call_llm_json


def find_ai_phrases(text: str) -> List[str]:
    """本地复查：返回简历/建议中命中的 AI 味词汇列表（纯逻辑）。"""
    if not text:
        return []
    return [phrase for phrase in MODIFY_AI_PHRASES if phrase in text]


def _build_skill_lines(role: Dict[str, Any]) -> str:
    """把岗位技能的七维命中明细格式化为 prompt 输入块。"""
    dims = role.get("dimensions") or {}
    lines = []
    for dim in ("knowledge", "skill", "qualifications", "preference",
                "motivation", "trait", "self_concept"):
        detail = dims.get(dim) or {}
        hit = detail.get("hit", [])
        miss = detail.get("miss", [])
        lines.append(
            f"- {dim}: 命中={hit or '无'} | 缺失={miss or '无'}"
        )
    return "\n".join(lines)


def prepare_resume_edit(role: Dict[str, Any], resume_text: str) -> Dict[str, Any]:
    """Agent 模式：准备简历修改提示包，不调用 LLM API。"""
    if not role or not role.get("role_name"):
        raise ValueError("role invalid: missing role_name")
    text = (resume_text or "").strip()
    ai_hint = "、".join(list(MODIFY_AI_PHRASES)[:8])
    prompt = MODIFY_PROMPT.format(
        role_name=role.get("role_name", ""),
        skill_lines=_build_skill_lines(role),
        resume_text=text[:12000],
        ai_phrase_hint=ai_hint,
    )
    return {
        "mode": "agent_edit",
        "purpose": "Write targeted resume edit suggestions with your own model",
        "prompt": prompt,
        "role_name": role.get("role_name", ""),
        "skill_lines": _build_skill_lines(role),
        "resume_text": text[:12000],
        "ai_phrase_blacklist": MODIFY_AI_PHRASES,
        "output_schema": {
            "target_role": "岗位名",
            "summary": "总体结论",
            "suggestions": [
                {
                    "skill": "技能名",
                    "status": "hit | reinforce | missing",
                    "suggestion": "具体修改建议",
                    "example_rewrite": "可选改写示例",
                }
            ],
            "risks": ["真实性/风险提示"],
        },
    }


def format_modify_markdown(role_name: str, llm_result: Dict[str, Any]) -> str:
    """把 LLM 返回的修改建议格式化为可读 Markdown（纯逻辑）。"""
    md = [f"## {role_name} — 简历修改建议", ""]
    summary = llm_result.get("summary") or ""
    if summary:
        md += ["**总体结论**：" + summary, ""]

    suggestions = llm_result.get("suggestions") or []
    status_label = {"hit": "已命中", "reinforce": "可强化", "missing": "缺失"}
    for i, item in enumerate(suggestions, 1):
        skill = item.get("skill", f"建议 {i}")
        status = item.get("status", "")
        label = status_label.get(status, status)
        md.append(f"### {i}. {skill}（{label}）")
        md.append(item.get("suggestion", ""))
        rewrite = item.get("example_rewrite")
        if rewrite:
            md += ["", f"> 改写示例：{rewrite}"]
        md.append("")

    risks = llm_result.get("risks") or []
    if risks:
        md += ["### 风险提示", ""]
        md += [f"- {r}" for r in risks]
        md.append("")
    return "\n".join(md)


def suggest_resume_edit(
    role: Dict[str, Any],
    resume_text: str,
    llm_func: Optional[Any] = None,
) -> Dict[str, Any]:
    """CLI 模式：调用当前配置的 LLM 生成修改建议。"""
    text = (resume_text or "").strip()
    if not text:
        raise ValueError("resume_text 不能为空")
    if not role or not role.get("role_name"):
        raise ValueError("role invalid: missing role_name")

    payload = prepare_resume_edit(role, text)
    caller = llm_func or call_llm_json
    llm_result = caller(payload["prompt"])
    if not isinstance(llm_result, dict):
        raise RuntimeError("LLM 返回格式异常：期望 JSON 对象")
    return {
        "role_name": role.get("role_name", ""),
        "analysis": llm_result,
        "markdown": format_modify_markdown(role.get("role_name", ""), llm_result),
    }


def validate_resume_edit(
    role: Dict[str, Any],
    resume_text: str,
    edit_json: Dict[str, Any],
) -> Dict[str, Any]:
    """Agent 模式：对修改建议做防造假校验（纯逻辑，不调用 LLM）。

    校验技能地基、状态一致性、量化指标地基、AI 味词汇，
    返回 {"valid", "summary", "violations", "stats", "checklist"}。
    """
    if not role or not role.get("role_name"):
        raise ValueError("role invalid: missing role_name")
    if not isinstance(edit_json, dict):
        raise ValueError("edit_json invalid: expected an object")
    return validate_edit_suggestions(role, resume_text, edit_json)

