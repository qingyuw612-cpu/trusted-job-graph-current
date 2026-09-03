"""复核结果合并 — 纯逻辑，零外部依赖。

职责：
- 把 LLM / Agent 复核后的 hit/miss 明细合并回粗排结果
- 确定性地重算七维覆盖率、命中数与岗位得分（不信任模型自报数字）
"""

from typing import Any, Dict, List


def _merge_dimension(
    dim_key: str,
    raw_detail: Dict[str, Any],
    review_detail: Dict[str, Any],
) -> Dict[str, Any]:
    """合并单个维度的 hit/miss，重算 coverage / hit_count / miss_count / total。"""
    hit = review_detail.get("hit")
    miss = review_detail.get("miss")
    if hit is None:
        hit = raw_detail.get("hit", [])
    if miss is None:
        miss = raw_detail.get("miss", [])
    hit = [str(x) for x in hit]
    miss = [str(x) for x in miss]
    total = len(hit) + len(miss)
    return {
        "hit": hit,
        "miss": miss,
        "coverage": round(len(hit) / total, 4) if total else 0.0,
        "total": total,
        "hit_count": len(hit),
        "miss_count": len(miss),
    }


def merge_enhance_review(
    raw_rank_result: Dict[str, Any],
    review_json: Dict[str, Any],
) -> Dict[str, Any]:
    """把复核 JSON 合并进粗排结果，返回与 enhance_matches 相同结构的 JSON。

    Args:
        raw_rank_result: rank_resume() 的完整返回（含每维 total/coverage）。
        review_json: 复核结果，results[i] 含 role_name / review_note /
                     dimensions.{dim}.{hit,miss}（未给出的维度回退到原始值）。

    Returns:
        {"topk": N, "results": [{role_name, score, hit_skills, total_skills,
                                 review_note, dimensions}, ...]}
    """
    raw_by_name = {r.get("role_name"): r for r in raw_rank_result.get("results", [])}
    results: List[Dict[str, Any]] = []

    for item in review_json.get("results", []):
        name = item.get("role_name")
        raw = raw_by_name.get(name)
        if not raw:
            continue

        review_dims = item.get("dimensions") or {}
        merged_dims: Dict[str, Dict[str, Any]] = {}
        hit_names: List[str] = []
        total_names: List[str] = []
        for dim, detail in (raw.get("dimensions") or {}).items():
            merged_dims[dim] = _merge_dimension(dim, detail, review_dims.get(dim) or {})
            hit_names.extend(merged_dims[dim]["hit"])
            total_names.extend(merged_dims[dim]["hit"] + merged_dims[dim]["miss"])

        dim_total = sum(d["total"] for d in merged_dims.values())
        total_skills = max(dim_total, raw.get("total_skills", 0) or 0)
        hit_skills = len(hit_names)
        weights = raw.get("skill_weights") or {}
        total_weight = sum(float(weights.get(n, 0.0)) for n in total_names)
        hit_weight = sum(float(weights.get(n, 0.0)) for n in hit_names)
        if total_weight > 0:
            coverage = hit_weight / total_weight
        else:
            coverage = hit_skills / max(total_skills, 1)
        penalty = min(1.0, total_skills / 10.0)
        score = round(coverage * penalty, 4)

        results.append(
            {
                "role_name": name,
                "score": score,
                "hit_skills": hit_skills,
                "total_skills": total_skills,
                "review_note": item.get("review_note", ""),
                "skill_weights": raw.get("skill_weights") or {},
                "dimensions": merged_dims,
            }
        )

    return {"topk": len(results), "results": results}


def check_review_structure(review_json: Dict[str, Any]) -> List[str]:
    """轻量校验复核 JSON，返回问题列表（空列表 = 结构可用）。"""
    problems: List[str] = []
    results = review_json.get("results")
    if not isinstance(results, list):
        return ["results 必须是数组"]
    for i, item in enumerate(results):
        if not isinstance(item, dict):
            problems.append(f"results[{i}] 不是对象")
            continue
        if not item.get("role_name"):
            problems.append(f"results[{i}] 缺少 role_name")
    return problems

