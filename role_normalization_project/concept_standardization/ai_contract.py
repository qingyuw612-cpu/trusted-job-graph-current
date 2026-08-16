"""AI岗位概念判断结果的严格输入契约。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .engine import DECISIONS


@dataclass(frozen=True)
class AIDecision:
    candidate_id: str
    decision: str
    target_role_id: str
    canonical_name: str
    parent_role_id: str
    tags: list[str]
    confidence: float
    reason: str
    model_version: str

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AIDecision":
        candidate_id = str(payload.get("candidate_id") or "").strip()
        decision = str(payload.get("decision") or "").strip()
        if not candidate_id.startswith("candidate:"):
            raise ValueError("candidate_id 格式无效")
        if decision not in DECISIONS:
            raise ValueError(f"未知 decision：{decision}")
        try:
            confidence = float(payload.get("confidence"))
        except (TypeError, ValueError) as exc:
            raise ValueError("confidence 必须是0到1之间的数字") from exc
        if not 0 <= confidence <= 1:
            raise ValueError("confidence 必须位于0到1之间")
        target = str(payload.get("target_role_id") or "").strip()
        canonical = str(payload.get("canonical_name") or "").strip()
        parent = str(payload.get("parent_role_id") or "").strip()
        if decision in {"EXISTING_ROLE", "ALIAS", "SUBROLE_OF"} and not target:
            raise ValueError(f"{decision} 必须提供 target_role_id")
        if decision == "NEW_ROLE_CANDIDATE" and not canonical:
            raise ValueError("NEW_ROLE_CANDIDATE 必须提供 canonical_name")
        raw_tags = payload.get("tags", [])
        if not isinstance(raw_tags, list):
            raise ValueError("tags 必须是数组")
        reason = str(payload.get("reason") or "").strip()
        if len(reason) < 10:
            raise ValueError("reason 必须给出可审核的职责与能力依据")
        return cls(
            candidate_id=candidate_id, decision=decision, target_role_id=target,
            canonical_name=canonical, parent_role_id=parent,
            tags=[str(x).strip() for x in raw_tags if str(x).strip()], confidence=confidence,
            reason=reason, model_version=str(payload.get("model_version") or "unspecified").strip(),
        )
