"""按既有 singleton 口径处理猎聘最后的待观察记录。

单例只能映射已批准岗位，不能创建新岗位；宽泛或冲突证据保持待观察。
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(".")
PROJECT = ROOT / "role_normalization_project"
OUTPUT = ROOT / "output" / "liepin_role_normalization"
SECOND = OUTPUT / "second_round"
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from concept_standardization.ai_contract import AIDecision  # noqa: E402
from apply_liepin_second_round_ai_defaults import decide, read_csv, write_csv  # noqa: E402


MODEL_VERSION = "codex-singleton-v2-liepin-2026-08-12"


def main() -> int:
    registry = json.loads((OUTPUT / "historical_approved_registry.json").read_text(encoding="utf-8"))
    role_by_name = {str(item["canonical_name"]): str(item["role_id"]) for item in registry["roles"]}
    for row in read_csv(OUTPUT / "new_roles_ai_approved.csv"):
        role_by_name[row["canonical_name"]] = row["role_id"]
    proposed = read_csv(OUTPUT / "proposed_role_assignments.csv")
    input_by_id = {row["职位ID"]: row for row in read_csv(SECOND / "pending_input.csv")}
    decisions: list[dict[str, Any]] = []
    contract_rows: list[AIDecision] = []
    for proposal in proposed:
        if proposal["assignment_status"] != "PENDING":
            continue
        row = input_by_id.get(proposal["version_id"], {})
        title = str(row.get("原始职位名称") or proposal.get("title") or "")
        candidate = str(row.get("岗位名称") or title)
        skills = str(row.get("能力提取结果") or "")
        dtype, target, confidence, reason = decide(title, candidate, skills, role_by_name)
        # 单例比跨企业簇更保守；过宽名称和低置信度不映射，非IT只做审计不写岗位。
        if dtype == "NON_IT" or confidence < 0.82:
            dtype, target = "INSUFFICIENT_INFO", ""
            confidence = min(confidence, 0.70)
            reason = "单条记录证据不足或不属于当前IT岗位范围，保留待观察，单例不创建新岗位。"
        target_id = role_by_name.get(target, "")
        cid = "candidate:singleton:" + proposal["version_id"].split(":", 1)[-1][:20]
        payload = {
            "candidate_id": cid, "decision": dtype, "target_role_id": target_id,
            "canonical_name": target or title, "parent_role_id": target_id if dtype == "SUBROLE_OF" else "",
            "tags": [], "confidence": confidence, "reason": reason, "model_version": MODEL_VERSION,
        }
        contract_rows.append(AIDecision.from_dict(payload))
        decisions.append({
            "version_id": proposal["version_id"], "title": title, "decision": dtype,
            "role_id": target_id, "canonical_name": target, "confidence": f"{confidence:.4f}",
            "reason": reason, "model_version": MODEL_VERSION,
        })
        if dtype != "INSUFFICIENT_INFO" and target_id:
            proposal.update({
                "assignment_status": "MAPPED", "role_id": target_id, "canonical_name": target,
                "decision": dtype, "confidence": f"{confidence:.4f}",
                "provenance": MODEL_VERSION + ":SINGLETON",
            })
    write_csv(OUTPUT / "proposed_role_assignments.csv", proposed, list(proposed[0].keys()))
    write_csv(SECOND / "singleton_ai_decisions.csv", decisions, [
        "version_id", "title", "decision", "role_id", "canonical_name", "confidence", "reason", "model_version",
    ])
    with (SECOND / "singleton_ai_contract.jsonl").open("w", encoding="utf-8") as stream:
        for item in contract_rows:
            stream.write(json.dumps(item.__dict__, ensure_ascii=False) + "\n")
    result = {
        "input_pending_records": len(decisions),
        "decisions": dict(Counter(row["decision"] for row in decisions)),
        "resolved_records": sum(row["assignment_status"] == "MAPPED" and row["provenance"].endswith(":SINGLETON") for row in proposed),
        "remaining_pending_records": sum(row["assignment_status"] == "PENDING" for row in proposed),
        "new_roles_created": 0, "graph_written": False,
    }
    (SECOND / "singleton_ai_manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
