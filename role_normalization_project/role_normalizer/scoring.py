"""岗位候选的相似度计算和混合评分。"""

from __future__ import annotations

import math
import re
from difflib import SequenceMatcher
from typing import Any, Mapping, Sequence

from .models import MatchScores


SCORE_KEYS = (
    "title_similarity",
    "responsibility_similarity",
    "skill_similarity",
    "lexical_similarity",
)


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """计算两个向量的余弦相似度，并将结果限制在 0～1。"""

    if len(left) == 0 or len(right) == 0 or len(left) != len(right):
        return 0.0
    # SentenceTransformer 返回 numpy 数组时走底层向量运算，避免在候选聚类中
    # 对数十万候选对执行逐元素 Python 循环。
    if hasattr(left, "dtype") and hasattr(right, "dtype"):
        try:
            import numpy as np

            left_array = np.asarray(left)
            right_array = np.asarray(right)
            denominator = float(np.linalg.norm(left_array) * np.linalg.norm(right_array))
            if denominator == 0.0:
                return 0.0
            value = float(np.dot(left_array, right_array)) / denominator
            return max(0.0, min(1.0, value))
        except ImportError:
            pass
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    value = sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)
    return max(0.0, min(1.0, value))


def normalize_for_lexical_match(text: str) -> str:
    """移除不影响字面比较的标点和空白。"""

    return re.sub(r"[^0-9a-z\u4e00-\u9fff+#]", "", str(text or "").casefold())


def lexical_similarity(left: str, right: str) -> float:
    """计算岗位名称的字面相似度。"""

    normalized_left = normalize_for_lexical_match(left)
    normalized_right = normalize_for_lexical_match(right)
    if not normalized_left or not normalized_right:
        return 0.0
    if normalized_left == normalized_right:
        return 1.0
    return SequenceMatcher(None, normalized_left, normalized_right).ratio()


def validate_weights(weights: Mapping[str, float]) -> dict[str, float]:
    """校验并归一化四项混合权重。"""

    aliases = {
        "title_similarity": "title",
        "responsibility_similarity": "responsibility",
        "skill_similarity": "skill",
        "lexical_similarity": "lexical",
    }
    missing = [
        key for key in SCORE_KEYS
        if key not in weights and aliases[key] not in weights
    ]
    if missing:
        raise ValueError(f"评分权重缺少字段：{', '.join(missing)}")
    values = {
        key: float(weights[key] if key in weights else weights[aliases[key]])
        for key in SCORE_KEYS
    }
    if any(value < 0.0 for value in values.values()):
        raise ValueError("评分权重不能为负数")
    total = sum(values.values())
    if total <= 0.0:
        raise ValueError("评分权重总和必须大于 0")
    return {key: value / total for key, value in values.items()}


def build_match_scores(
    *,
    title_similarity: float,
    responsibility_similarity: float,
    skill_similarity: float,
    lexical_similarity_value: float,
    weights: Mapping[str, float],
) -> MatchScores:
    """按配置权重组合各项得分，构造统一评分对象。"""

    normalized_weights = validate_weights(weights)
    components = {
        "title_similarity": max(0.0, min(1.0, float(title_similarity))),
        "responsibility_similarity": max(0.0, min(1.0, float(responsibility_similarity))),
        "skill_similarity": max(0.0, min(1.0, float(skill_similarity))),
        "lexical_similarity": max(0.0, min(1.0, float(lexical_similarity_value))),
    }
    combined = sum(components[key] * normalized_weights[key] for key in SCORE_KEYS)
    return MatchScores(**components, combined_similarity=combined)


def detect_function_conflict(
    source_text: str,
    target_text: str,
    config: Mapping[str, Any] | None,
) -> tuple[bool, str]:
    """根据外部配置识别开发、测试等岗位功能词冲突。

    配置支持两种形式：`function_terms` 将类别映射到关键词列表，
    `function_conflicts` 列出互斥类别对；若未提供互斥类别对，则所有不同
    类别均视为冲突。模块本身不内置业务关键词。
    """

    if not config:
        return False, ""
    term_groups = config.get("function_terms", {})
    if not isinstance(term_groups, Mapping):
        raise ValueError("function_terms 必须是类别到关键词列表的映射")

    def categories(text: str) -> set[str]:
        folded = str(text or "").casefold()
        return {
            str(category)
            for category, terms in term_groups.items()
            if any(str(term).casefold() in folded for term in terms if str(term))
        }

    source_categories = categories(source_text)
    target_categories = categories(target_text)
    if not source_categories or not target_categories or source_categories & target_categories:
        return False, ""

    configured_pairs = config.get("function_conflicts")
    if configured_pairs:
        conflict_pairs = {frozenset(map(str, pair)) for pair in configured_pairs if len(pair) == 2}
        conflict = any(
            frozenset((source, target)) in conflict_pairs
            for source in source_categories
            for target in target_categories
        )
    else:
        conflict = True
    if not conflict:
        return False, ""
    detail = f"功能词冲突：{sorted(source_categories)} -> {sorted(target_categories)}"
    return True, detail
