from __future__ import annotations

import re
from typing import Any


_NOISE = re.compile(
    r"我们正在寻找|加入我们|公司简介|平台介绍|职位描述|岗位职责\s*[:：]?$|"
    r"工作地点|base\s*[:：]?|薪资|福利|学历|工作年限|年以上|经验优先|"
    r"熟悉.+流程|熟练使用|相关专业",
    re.IGNORECASE,
)
_PUNCTUATION = re.compile(r"[\s，。；：、,.!！?？()（）【】\[\]\"'“”‘’]")


def _normalized(value: Any) -> str:
    return _PUNCTUATION.sub("", str(value or "").lower())


def _condense(text: str) -> str:
    value = re.sub(r"^[#>*\-\s]+", "", text).strip(" \t\r\n；;。")
    if not value or _NOISE.search(value):
        return ""
    if re.search(r"(?:工程师|产品经理|测试|专家)$", value) and not re.search(
        r"设计|建设|开发|制定|规划|分析|编写|维护|优化|对接|解决",
        value,
    ):
        return ""
    if re.match(r"^(?:了解|熟悉|有\s*\d+\s*年)", value):
        return ""
    value = re.sub(r"^(?:业务职责|客户技术支持|需求分析与选型评估)\s*[:：]\s*", "", value)
    value = re.sub(r"^负责", "", value)
    value = re.sub(r"^主导", "推进", value)
    value = re.sub(r"^深度对接.+?[，,]", "", value)
    value = re.sub(r"^根据.+?(?=制定|设计|编写)", "", value)
    value = re.sub(r"^基于.+?(?=编写|建设|设计)", "", value)
    value = re.sub(
        r"自动驾驶车辆端软件与云端、车内硬件及驾驶员之间的桥梁搭建",
        "构建车端软件与云端、车内硬件及驾驶员之间的交互链路",
        value,
    )
    value = re.sub(r"（[^（）]+）", "", value)
    value = re.sub(r"\([^()]+\)", "", value)
    clauses = [part.strip() for part in re.split(r"[；;。]", value) if part.strip()]
    if not clauses:
        return ""
    summary = clauses[0]
    comma_parts = [part.strip() for part in re.split(r"[，,]", summary) if part.strip()]
    if len(summary) > 58 and comma_parts:
        summary = "，".join(comma_parts[:2])
    summary = re.sub(r"，?(?:保障|确保|提升)客户.+$", "", summary)
    summary = re.sub(r"，?支撑亿级.+$", "", summary)
    return summary[:80].strip(" ，,；;。")


def summarize_responsibility_evidence(
    evidence: Any,
    *,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Build a concise, evidence-bound fallback when AI duties are unavailable."""
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in evidence or []:
        if not isinstance(item, dict):
            continue
        raw_text = str(item.get("text") or "").strip()
        jd_id = str(item.get("jd_id") or "").strip()
        summary = _condense(raw_text)
        normalized = _normalized(summary)
        if not summary or not jd_id or len(summary) < 8 or normalized in seen:
            continue
        if normalized == _normalized(raw_text):
            summary = summary.replace("设计并实现", "设计与实现", 1)
            if _normalized(summary) == _normalized(raw_text):
                summary = f"持续推进{summary}"
            normalized = _normalized(summary)
        output.append({"text": summary, "evidence_jd_ids": [jd_id]})
        seen.add(normalized)
        if len(output) >= limit:
            break
    return output
