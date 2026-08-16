"""岗位名称归一化项目的 JSON 配置加载器。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "default.json"


@dataclass(frozen=True)
class MatchingWeights:
    """名称、职责、技能和字面相似度的组合权重。"""

    title: float = 0.35
    responsibility: float = 0.25
    skill: float = 0.30
    lexical: float = 0.10

    def to_dict(self) -> Dict[str, float]:
        """返回权重配置字典。"""

        return {
            "title": self.title,
            "responsibility": self.responsibility,
            "skill": self.skill,
            "lexical": self.lexical,
        }


@dataclass(frozen=True)
class MatchingThresholds:
    """自动映射、人工审核和新岗位候选的初始阈值。"""

    auto_match: float = 0.93
    review: float = 0.82
    new_role_min_jds: int = 3
    new_role_min_companies: int = 3
    new_role_min_templates: int = 3
    new_role_min_skills: int = 3

    def to_dict(self) -> Dict[str, Any]:
        """返回阈值配置字典。"""

        return {
            "auto_match": self.auto_match,
            "review": self.review,
            "new_role_min_jds": self.new_role_min_jds,
            "new_role_min_companies": self.new_role_min_companies,
            "new_role_min_templates": self.new_role_min_templates,
            "new_role_min_skills": self.new_role_min_skills,
        }


@dataclass(frozen=True)
class RoleNormalizerConfig:
    """归一化项目的顶层配置对象。"""

    version: str
    embedding_model: str
    top_k: int
    weights: MatchingWeights
    thresholds: MatchingThresholds
    hard_constraint_terms: Dict[str, list[str]] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        """返回配置的核心运行参数。"""

        return {
            "version": self.version,
            "embedding_model": self.embedding_model,
            "top_k": self.top_k,
            "weights": self.weights.to_dict(),
            "thresholds": self.thresholds.to_dict(),
            "hard_constraint_terms": {
                key: list(values) for key, values in self.hard_constraint_terms.items()
            },
        }


def _validate_config(config: RoleNormalizerConfig) -> None:
    """验证权重、阈值和召回数量，尽早暴露配置错误。"""

    weight_sum = sum(config.weights.to_dict().values())
    if abs(weight_sum - 1.0) > 1e-6:
        raise ValueError(f"匹配权重之和必须为 1.0，当前为 {weight_sum:.6f}")
    if not 0.0 <= config.thresholds.review <= config.thresholds.auto_match <= 1.0:
        raise ValueError("阈值必须满足 0 <= review <= auto_match <= 1")
    if config.top_k < 1:
        raise ValueError("top_k 必须大于等于 1")


def load_config(path: str | Path | None = None) -> RoleNormalizerConfig:
    """从 JSON 文件加载并验证运行配置；不传路径时使用默认配置。"""

    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    with config_path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    matching = data.get("matching", {})
    weights_data = matching.get("weights", {})
    threshold_data = matching.get("thresholds", {})
    config = RoleNormalizerConfig(
        version=str(data.get("version", "1.0.0")),
        embedding_model=str(data.get("embedding_model", "")),
        top_k=int(matching.get("top_k", 5)),
        weights=MatchingWeights(
            title=float(weights_data.get("title", 0.35)),
            responsibility=float(weights_data.get("responsibility", 0.25)),
            skill=float(weights_data.get("skill", 0.30)),
            lexical=float(weights_data.get("lexical", 0.10)),
        ),
        thresholds=MatchingThresholds(
            auto_match=float(threshold_data.get("auto_match", 0.93)),
            review=float(threshold_data.get("review", 0.82)),
            new_role_min_jds=int(threshold_data.get("new_role_min_jds", 3)),
            new_role_min_companies=int(threshold_data.get("new_role_min_companies", 3)),
            new_role_min_templates=int(threshold_data.get("new_role_min_templates", 3)),
            new_role_min_skills=int(threshold_data.get("new_role_min_skills", 3)),
        ),
        hard_constraint_terms={
            str(key): [str(item) for item in values]
            for key, values in data.get("hard_constraint_terms", {}).items()
        },
        raw=dict(data),
    )
    _validate_config(config)
    return config
