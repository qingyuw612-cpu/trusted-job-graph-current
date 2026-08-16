"""校验并导入外部AI判断；不会将任何决策标记为人工批准。"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from concept_standardization.ai_contract import AIDecision
from concept_standardization.engine import ConceptStandardizationEngine


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--decisions", type=Path, required=True, help="AI输出JSONL")
    parser.add_argument("--review-queue", type=Path, required=True)
    args = parser.parse_args()
    with args.review_queue.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    by_id = {row["candidate_id"]: row for row in rows}
    imported = 0
    with args.decisions.open("r", encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                decision = AIDecision.from_dict(json.loads(line))
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"AI结果第{line_no}行无效：{exc}") from exc
            if decision.candidate_id not in by_id:
                raise KeyError(f"AI结果引用未知候选：{decision.candidate_id}")
            row = by_id[decision.candidate_id]
            row.update({
                "ai_decision": decision.decision,
                "ai_target_role_id": decision.target_role_id,
                "ai_canonical_name": decision.canonical_name,
                "ai_parent_role_id": decision.parent_role_id,
                "ai_tags": "；".join(decision.tags),
                "ai_confidence": f"{decision.confidence:.4f}",
                "ai_reason": decision.reason,
                "ai_model_version": decision.model_version,
            })
            imported += 1
    with args.review_queue.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=ConceptStandardizationEngine.REVIEW_FIELDS, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)
    print(json.dumps({"imported": imported, "review_status_changed": 0}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
