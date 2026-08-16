"""可选的大模型岗位概念审核器。

默认不调用网络。启用时读取环境变量 ROLE_AI_REVIEW_ENABLED=1、
ROLE_AI_API_KEY、ROLE_AI_BASE_URL 和 ROLE_AI_MODEL。
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

DECISIONS = {"EXISTING_ROLE", "ALIAS", "SUBROLE_OF", "NEW_ROLE_CANDIDATE", "NON_IT", "NOISE", "INSUFFICIENT_INFO"}


class AIRoleJudge:
    def __init__(self) -> None:
        self.enabled = os.getenv("ROLE_AI_REVIEW_ENABLED", "0").lower() in {"1", "true", "yes"}
        self.api_key = os.getenv("ROLE_AI_API_KEY", "").strip()
        self.base_url = os.getenv("ROLE_AI_BASE_URL", "https://api.openai.com/v1/chat/completions").strip()
        self.model = os.getenv("ROLE_AI_MODEL", "gpt-4o-mini").strip()
        self.max_tokens = int(os.getenv("ROLE_AI_MAX_TOKENS", "800"))

    def judge(self, evidence: dict[str, Any], roles: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.enabled:
            return {"status": "SKIPPED", "reason": "ROLE_AI_REVIEW_DISABLED"}
        if not self.api_key:
            return {"status": "SKIPPED", "reason": "ROLE_AI_API_KEY_MISSING"}
        prompt = {
            "task": "岗位概念标准化审核",
            "rules": [
                "不能只凭岗位名称判断，必须结合职责、技能、JD数量和企业数量",
                "技术栈、行业和等级通常使用标签或子岗位，不直接新建平级岗位",
                "NEW_ROLE_CANDIDATE 只是候选，不能批准正式岗位",
                "证据不足必须返回 INSUFFICIENT_INFO",
            ],
            "decision_enum": sorted(DECISIONS),
            "evidence": evidence,
            "nearest_existing_roles": roles[:5],
            "output_schema": {"decision": "", "target_role_id": "", "canonical_name": "", "parent_role_id": "", "tags": [], "confidence": 0.0, "reason": "", "missing_evidence": [], "requires_human_review": True},
        }
        body = json.dumps({"model": self.model, "temperature": 0, "max_tokens": self.max_tokens, "messages": [
            {"role": "system", "content": "你是保守的岗位分类审核员，只返回合法JSON，不输出Markdown。"},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ]}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.base_url, data=body, method="POST", headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
            content = payload["choices"][0]["message"]["content"]
            result = json.loads(content)
            if result.get("decision") not in DECISIONS:
                raise ValueError("invalid decision")
            result["confidence"] = max(0.0, min(1.0, float(result.get("confidence") or 0)))
            result["requires_human_review"] = True
            return {"status": "COMPLETED", "analysis": result}
        except Exception as exc:  # network/provider/model errors remain auditable
            return {"status": "FAILED", "reason": type(exc).__name__ + ":" + str(exc)[:240]}
