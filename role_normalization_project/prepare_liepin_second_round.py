"""把猎聘待观察记录适配为既有第二轮语义聚类输入。"""

from __future__ import annotations

import csv
import json
from pathlib import Path


ROOT = Path(".")
OUTPUT = ROOT / "output" / "liepin_role_normalization"
BGE = OUTPUT / "bge_run"
SECOND = OUTPUT / "second_round"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def chinese_ability(raw: str) -> str:
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        payload = {}
    return json.dumps(
        {
            "技术": list(payload.get("Skill") or payload.get("技能") or []),
            "知识": list(payload.get("Knowledge") or payload.get("知识") or []),
        },
        ensure_ascii=False,
    )


def main() -> int:
    SECOND.mkdir(parents=True, exist_ok=True)
    proposed = {row["version_id"]: row for row in read_csv(OUTPUT / "proposed_role_assignments.csv")}
    source_rows = read_csv(BGE / "role_resolutions.csv")
    input_rows: list[dict[str, str]] = []
    mapping_rows: list[dict[str, str]] = []
    for row in source_rows:
        proposal = proposed.get(row["version_id"], {})
        if proposal.get("assignment_status") != "PENDING":
            continue
        input_rows.append(
            {
                "职位ID": row["version_id"],
                "岗位名称": row.get("规则归一化名称", ""),
                "原始职位名称": row.get("title", ""),
                "JD全文": row.get("description", ""),
                "能力提取结果": chinese_ability(row.get("ability_analysis_raw", "")),
                "搜索关键词": row.get("source_platform", "猎聘"),
                "公司全称": row.get("company_name", "") or row.get("company_id", ""),
            }
        )
        mapping_rows.append({"职位ID": row["version_id"], "匹配类型": "PENDING"})
    write_csv(
        SECOND / "pending_input.csv",
        input_rows,
        ["职位ID", "岗位名称", "原始职位名称", "JD全文", "能力提取结果", "搜索关键词", "公司全称"],
    )
    write_csv(SECOND / "pending_mapping.csv", mapping_rows, ["职位ID", "匹配类型"])
    manifest = {
        "records": len(input_rows),
        "mode": "ADAPTER_FOR_EXISTING_SECOND_ROUND",
        "input": str(SECOND / "pending_input.csv"),
        "mapping": str(SECOND / "pending_mapping.csv"),
    }
    (SECOND / "adapter_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
