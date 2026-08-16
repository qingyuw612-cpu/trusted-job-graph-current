"""端到端验证猎聘岗位大模型归一化发布结果。"""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(".")
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from trusted_graph_agent.neo4j_repository import Neo4jGraphRepository  # noqa: E402


SOURCE_ID = "rawsource:6916d9be33e842c4460bf43a9cefe4f44a27d420"
RUN_ID = "normalization:a2fa9df80c90bd608ade"
NAMES = ["射频工程师", "5G核心网研发工程师", "具身智能系统工程师", "数据挖掘工程师", "量化开发工程师", "雷达系统工程师"]


def main() -> int:
    repository = Neo4jGraphRepository(ROOT / "config" / "neo4j_connection.json")
    active = repository.client.query(
        "MATCH (:NormalizationPointer {name:'core'})-[:ACTIVE]->(run) RETURN run.run_id AS run_id,run.status AS status"
    )
    liepin = repository.client.query(
        """
        MATCH (raw:RawJDVersion)-[:FROM_SOURCE]->(:RawSourceFile {source_file_id:$source_id})
        RETURN count(raw) AS total,
               count(CASE WHEN raw.domain_label='IT' THEN 1 END) AS it,
               count(CASE WHEN raw.domain_label='IT' AND coalesce(raw.domain_role,'')<>'' THEN 1 END) AS mapped,
               count(CASE WHEN raw.domain_label='IT' AND coalesce(raw.domain_role,'')='' THEN 1 END) AS pending,
               count(CASE WHEN raw.domain_role_locked=true THEN 1 END) AS ai_locked
        """, {"source_id": SOURCE_ID}
    )
    new_roles = repository.client.query(
        """
        UNWIND $names AS name
        MATCH (role:Role {name:name})
        OPTIONAL MATCH (role)-[edge:HAS_CORE_SKILL {run_id:$run_id}]->(:NormalizedSkill)
        WITH name, role, count(DISTINCT edge) AS core_skills
        OPTIONAL MATCH (raw:RawJDVersion {standard_role_id:role.role_id})
          -[:HAS_PROCESSING_RESULT]->(:ProcessedJD {status:'COMPLETED'})
        RETURN name, role.role_id AS role_id, core_skills, count(DISTINCT raw) AS jds
        ORDER BY name
        """, {"names": NAMES, "run_id": RUN_ID}
    )
    errors = []
    if not active or active[0].get("run_id") != RUN_ID or active[0].get("status") != "ACTIVE": errors.append("活动版本不正确")
    if not liepin or int(liepin[0].get("ai_locked") or 0) != 3129: errors.append("猎聘AI锁定数量不正确")
    if len(new_roles) != len(NAMES): errors.append("新增岗位节点不完整")
    for row in new_roles:
        if int(row.get("core_skills") or 0) <= 0 or int(row.get("jds") or 0) <= 0:
            errors.append(f"新增岗位缺少JD或核心技能：{row.get('name')}")
    result = {"active": active[0] if active else {}, "liepin": liepin[0] if liepin else {}, "new_roles": new_roles, "errors": errors, "valid": not errors}
    (OUTPUT := ROOT / "output" / "liepin_role_normalization" / "final_verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__": raise SystemExit(main())
