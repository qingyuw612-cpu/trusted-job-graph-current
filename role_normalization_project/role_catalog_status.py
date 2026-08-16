"""用通俗中文显示当前处理进度。"""
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path


PROJECT = Path(__file__).resolve().parent
WORKSPACE = PROJECT.parents[1]
RESULTS = WORKSPACE / "2026数据51job" / "岗位概念标准化结果"


def main() -> int:
    final_manifest = RESULTS / "final_manifest.json"
    if final_manifest.exists():
        data = json.loads(final_manifest.read_text(encoding="utf-8"))
        print("岗位标准化当前进度")
        print(f"- 招聘记录：{data.get('input_rows', 0)}")
        print(f"- 已归类记录：{data.get('resolved_rows', 0)}")
        print(f"- 待观察记录：{data.get('observation_rows', 0)}")
        print(f"- 覆盖率：{float(data.get('coverage', 0)):.2%}")
        print(f"- 岗位主表概念：{data.get('role_master_count', 0)}")
        print("- AI结论：默认生效，可人工覆盖")
        print("- Neo4j写入：否")
        return 0
    manifest = RESULTS / "concept_manifest.json"
    if not manifest.exists():
        print("尚未生成结果，请先双击“01_准备岗位审核.cmd”。")
        return 1
    data = json.loads(manifest.read_text(encoding="utf-8"))
    queue = RESULTS / "concept_review_queue.csv"
    with queue.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    statuses = Counter(row.get("review_status") or "PENDING" for row in rows)
    ai_count = sum(bool(row.get("ai_decision")) for row in rows)
    print("岗位标准化当前进度")
    print(f"- 招聘记录：{data.get('input_rows', 0)}")
    print(f"- 已映射记录：{data.get('mapped_job_rows', 0)}")
    print(f"- 待处理名称：{data.get('unresolved_names', 0)}")
    print(f"- 已导入AI建议：{ai_count}")
    print(f"- 人工已批准：{statuses.get('APPROVED', 0)}")
    print(f"- 保留观察：{statuses.get('OBSERVE', 0)}")
    print(f"- 正式批准新岗位：{data.get('new_roles_approved', 0)}")
    print("- Neo4j写入：否")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
