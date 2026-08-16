"""发布前校验人工审核表，不修改任何数据。"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from concept_standardization.engine import FINAL_DECISIONS
from role_normalizer.taxonomy_adapter import load_role_registry


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-queue", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    args = parser.parse_args()
    registry = load_role_registry(args.registry)
    known_ids = {role.role_id for role in registry}
    known_names = {role.canonical_name.casefold() for role in registry}
    with args.review_queue.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    errors, approved = [], 0
    new_names: set[str] = set()
    for line, row in enumerate(rows, start=2):
        if row.get("review_status") != "APPROVED":
            continue
        approved += 1
        decision = row.get("reviewer_decision", "")
        if decision not in FINAL_DECISIONS:
            errors.append(f"第{line}行：APPROVED但决策无效")
            continue
        target = row.get("reviewer_role_id") or row.get("suggested_role_id") or ""
        if decision in {"EXISTING_ROLE", "ALIAS", "SUBROLE_OF"} and target not in known_ids:
            errors.append(f"第{line}行：目标role_id不在受控岗位库")
        if decision == "NEW_ROLE_CANDIDATE":
            name = str(row.get("reviewer_canonical_name") or "").strip()
            if not name:
                errors.append(f"第{line}行：新岗位缺少标准名称")
            elif name.casefold() in known_names or name.casefold() in new_names:
                errors.append(f"第{line}行：新岗位名称与已有/本批岗位重复：{name}")
            else:
                new_names.add(name.casefold())
            if not str(row.get("reviewer_note") or "").strip():
                errors.append(f"第{line}行：新岗位缺少人工审核依据")
    report = {"rows": len(rows), "approved": approved, "errors": errors, "valid": not errors}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
