"""enhance_matches 工具 — 用一次 LLM 调用复核 Top-N 命中。"""

import json
from typing import Any, Dict, Optional

from ..prompts.enhance import ENHANCE_PROMPT
from ..core.review import check_review_structure, merge_enhance_review
from ..utils.llm import call_llm_json


def _trim_rank_result(rank_result: Dict[str, Any], topk: int) -> Dict[str, Any]:
    """截取 rank 结果的前 topk 名，并精简每项的维度明细（去掉 miss 列表冗余）。"""
    results = rank_result.get("results", [])[:topk]
    trimmed = []
    for item in results:
        dims = {}
        for dim, detail in (item.get("dimensions") or {}).items():
            dims[dim] = {
                "hit": detail.get("hit", []),
                "miss": detail.get("miss", []),
            }
        trimmed.append(
            {
                "role_name": item.get("role_name", ""),
                "score": item.get("score", 0.0),
                "hit_skills": item.get("hit_skills", 0),
                "total_skills": item.get("total_skills", 0),
                "dimensions": dims,
            }
        )
    return {"topk": len(trimmed), "results": trimmed}


def prepare_enhance(
    rank_result: Dict[str, Any],
    resume_text: str,
    topk: int = 20,
) -> Dict[str, Any]:
    """Agent mode: build the review payload without calling any LLM API.

    The agent uses its own model to perform the semantic review, then calls
    apply_enhance_review(rank_json=full rank_resume result, review_json=...)
    to merge and normalize.
    """
    trimmed = _trim_rank_result(rank_result, topk)
    text = (resume_text or "").strip()
    prompt = ENHANCE_PROMPT.format(
        topk=len(trimmed["results"]),
        resume_text=text[:12000],
        rank_json=json.dumps(trimmed, ensure_ascii=False),
    )
    return {
        "mode": "agent_review",
        "purpose": "Review the keyword ranking with your own model and fix false hits/misses",
        "prompt": prompt,
        "rank_data": trimmed,
        "resume_text": text[:12000],
        "output_schema": {
            "topk": "number of reviewed roles",
            "results": [
                {
                    "role_name": "must match input role name",
                    "score": "recomputed score 0~1",
                    "hit_skills": "hit count",
                    "total_skills": "total count",
                    "review_note": "what was corrected, or no-change note",
                    "dimensions": {
                        "knowledge/skill/qualifications/preference/motivation/trait/self_concept": {
                            "hit": ["skills truly demonstrated"],
                            "miss": ["skills missing"],
                        }
                    },
                }
            ],
        },
        "next_step": "call apply_enhance_review(rank_json, review_json) after reviewing",
    }


def apply_enhance_review(
    raw_rank_result: Dict[str, Any],
    review_json: Dict[str, Any],
) -> Dict[str, Any]:
    """Agent mode: merge the agent's review JSON back into the rank result.

    Pure logic, no LLM call. Recomputes coverage and scores deterministically.
    """
    if not raw_rank_result or not raw_rank_result.get("results"):
        raise ValueError("raw_rank_result is empty; run rank_resume first")
    if not isinstance(review_json, dict) or not review_json.get("results"):
        raise ValueError("review_json invalid: expected a results array")
    problems = check_review_structure(review_json)
    if problems:
        raise ValueError("review_json 结构异常: " + "; ".join(problems))
    return merge_enhance_review(raw_rank_result, review_json)


def enhance_matches(
    rank_result: Dict[str, Any],
    resume_text: str,
    topk: int = 20,
    llm_func: Optional[Any] = None,
) -> Dict[str, Any]:
    """调用 LLM 复核排名结果，修正关键词命中的误判。

    Args:
        rank_result: rank_resume() 的返回（含 results 列表）。
        resume_text: 简历 Markdown 原文。
        topk: 复核前 N 名，默认 20。
        llm_func: 可注入的 LLM 调用函数（测试用），缺省用 call_llm_json。

    Returns:
        LLM 修正后的 JSON：
        {"topk": N, "results": [{"role_name", "score", "hit_skills",
                                 "total_skills", "review_note", "dimensions"}, ...]}
    """
    text = (resume_text or "").strip()
    if not text:
        raise ValueError("resume_text 不能为空。")
    if not rank_result or not rank_result.get("results"):
        raise ValueError("rank_result 为空，请先调用 rank_resume。")

    trimmed = _trim_rank_result(rank_result, topk)
    prompt = ENHANCE_PROMPT.format(
        topk=len(trimmed["results"]),
        resume_text=text[:12000],  # 控制 token 成本
        rank_json=json.dumps(trimmed, ensure_ascii=False),
    )
    caller = llm_func or call_llm_json
    result = caller(prompt)
    if not isinstance(result, dict):
        raise RuntimeError("LLM 复核返回格式异常：期望 JSON 对象。")
    return result

