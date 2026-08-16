"""Prepare auditable, strict Neo4j import artifacts from role standardization CSVs.

Never connects unless --execute is explicitly supplied.
"""
from __future__ import annotations

import argparse, csv, json, shutil, sys
from collections import Counter
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]
PROJECT = AGENT_ROOT.parent
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))
RESULTS = PROJECT / "2026数据51job" / "岗位概念标准化结果"
DEFAULT_OUT = RESULTS / "neo4j_strict_import"
ROLE_FIELDS = ["role_id","name","family_id","parent_role_id","approval_status","source_batch","confidence","definition_version"]
JOB_FIELDS = ["jd_id","title","source_batch"]
EDGE_FIELDS = ["jd_id","role_id","match_type","approval_status","confidence","source_batch","definition_version"]

def read(path):
    with path.open(encoding="utf-8-sig", newline="") as f: return list(csv.DictReader(f))

def prepare(role_path=RESULTS/"role_master_draft.csv", mapping_path=RESULTS/"job_role_mapping_draft.csv", include_ai=False):
    roles, mappings = read(role_path), read(mapping_path)
    allowed_roles = {"CONTROLLED", "HUMAN_APPROVED_NEW"}
    if include_ai:
        allowed_roles.update({"AI_APPROVED_NEW", "AI_APPROVED_NEW_ROUND2"})
    role_by_id = {r["role_id"]: r for r in roles if r.get("status") in allowed_roles and r.get("role_id")}
    strict = {("EXISTING_ROLE", "SYSTEM_APPROVED"), ("ALIAS", "HUMAN_APPROVED")}
    ai = {"AI_APPROVED", "AI_APPROVED_SINGLETON", "AI_APPROVED_ROUND2"}
    accepted, rejected = [], []
    for r in mappings:
        status, kind, rid = (r.get("审核状态") or "").strip(), (r.get("匹配类型") or "").strip(), (r.get("role_id") or "").strip()
        ok = (kind, status) in strict or (include_ai and rid and kind not in {"PENDING", "NON_IT"} and (status in ai or status == "HUMAN_APPROVED"))
        reason = "" if ok and rid in role_by_id else ("ROLE_NOT_HIGH_TRUST" if ok else ("NON_IT" if kind == "NON_IT" else status or kind or "MISSING_DECISION"))
        if ok and not reason:
            accepted.append(r)
        else:
            rejected.append({**r, "拒绝原因": reason})
    used = {r["role_id"] for r in accepted}
    role_rows = []
    for r in roles:
        if r.get("role_id") in used or r.get("status") in allowed_roles:
            role_rows.append({"role_id":r.get("role_id",""),"name":r.get("canonical_name",""),"family_id":r.get("family",""),"parent_role_id":r.get("parent_role_id",""),"approval_status":r.get("status",""),"source_batch":"role_master_draft","confidence":"1.0" if r.get("status")=="CONTROLLED" else "", "definition_version":r.get("definition_version","")})
    jobs = [{"jd_id":r.get("职位ID",""),"title":r.get("原始职位名称","").strip(),"source_batch":"job_role_mapping_draft"} for r in accepted]
    edges = [{"jd_id":r.get("职位ID",""),"role_id":r.get("role_id",""),"match_type":r.get("匹配类型",""),"approval_status":r.get("审核状态",""),"confidence":r.get("置信度",""),"source_batch":"job_role_mapping_draft","definition_version":r.get("定义版本","")} for r in accepted]
    return role_rows, jobs, edges, rejected, {"input_mappings":len(mappings),"roles":len(role_rows),"jobs":len(jobs),"relationships":len(edges),"rejected":len(rejected),"include_ai":include_ai}

def write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w=csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(rows)

def cypher(include_ai=False):
    return """// Generated import preview. Uses existing Role/JD/INSTANCE_OF conventions.
CREATE CONSTRAINT role_id IF NOT EXISTS FOR (n:Role) REQUIRE n.role_id IS UNIQUE;
CREATE CONSTRAINT jd_id IF NOT EXISTS FOR (n:JD) REQUIRE n.jd_id IS UNIQUE;
LOAD CSV WITH HEADERS FROM 'file:///role_normalization_roles.csv' AS row
MERGE (r:Role {role_id:row.role_id})
SET r.name=CASE WHEN coalesce(r.approval_status,'') IN ['HUMAN_APPROVED','HUMAN_APPROVED_NEW'] THEN r.name ELSE row.name END,
    r.family_id=coalesce(r.family_id,row.family_id), r.parent_role_id=coalesce(r.parent_role_id,row.parent_role_id),
    r.approval_status=coalesce(r.approval_status,row.approval_status), r.source_batch=coalesce(r.source_batch,row.source_batch),
    r.confidence=coalesce(r.confidence,row.confidence), r.definition_version=coalesce(r.definition_version,row.definition_version);
LOAD CSV WITH HEADERS FROM 'file:///role_normalization_jobs.csv' AS row
MERGE (j:JD {jd_id:row.jd_id}) SET j.title=coalesce(j.title,row.title), j.source_batch=coalesce(j.source_batch,row.source_batch);
LOAD CSV WITH HEADERS FROM 'file:///role_normalization_edges.csv' AS row
MATCH (j:JD {jd_id:row.jd_id}), (r:Role {role_id:row.role_id})
MERGE (j)-[e:INSTANCE_OF]->(r)
WITH row, e, coalesce(e.approval_status,'') STARTS WITH 'HUMAN_' AS keep_human
SET e.match_type=CASE WHEN keep_human THEN e.match_type ELSE row.match_type END,
    e.approval_status=CASE WHEN keep_human THEN e.approval_status ELSE row.approval_status END,
    e.confidence=CASE WHEN keep_human THEN e.confidence ELSE toFloat(row.confidence) END,
    e.source_batch=CASE WHEN keep_human THEN e.source_batch ELSE row.source_batch END,
    e.definition_version=CASE WHEN keep_human THEN e.definition_version ELSE row.definition_version END;
"""

def cypher_statements(text):
    """Return executable statements, excluding preview-only comment lines."""
    executable = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("//")
    )
    return [statement.strip() for statement in executable.split(";") if statement.strip()]

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("--check", action="store_true"); p.add_argument("--dry-run", action="store_true"); p.add_argument("--execute", action="store_true"); p.add_argument("--include-ai", action="store_true"); p.add_argument("--yes", action="store_true"); p.add_argument("--output", type=Path, default=DEFAULT_OUT); p.add_argument("--config", type=Path, default=AGENT_ROOT/"config"/"neo4j_connection.json")
    a=p.parse_args(argv)
    if a.execute and not a.config.exists(): raise SystemExit(f"Neo4j config not found: {a.config}")
    role_rows,jobs,edges,rejected,stats=prepare(include_ai=a.include_ai)
    if a.check: print(json.dumps(stats,ensure_ascii=False,indent=2)); return 0
    a.output.mkdir(parents=True,exist_ok=True); write_csv(a.output/"role_normalization_roles.csv",ROLE_FIELDS,role_rows); write_csv(a.output/"role_normalization_jobs.csv",JOB_FIELDS,jobs); write_csv(a.output/"role_normalization_edges.csv",EDGE_FIELDS,edges); write_csv(a.output/"rejected.csv",list(rejected[0].keys()) if rejected else ["拒绝原因"],rejected)
    (a.output/"import.cypher").write_text(cypher(a.include_ai),encoding="utf-8"); (a.output/"stats.json").write_text(json.dumps(stats,ensure_ascii=False,indent=2),encoding="utf-8")
    if a.execute:
        from trusted_graph_agent.neo4j_repository import Neo4jHttpClient
        config=json.loads(a.config.read_text(encoding="utf-8"))
        required={"http_uri","password","import_dir"}; missing=sorted(required-set(config))
        if missing: raise SystemExit(f"Neo4j config missing: {', '.join(missing)}")
        if not a.yes:
            answer=input(f"即将更新 Neo4j：Role={stats['roles']}，Job/关系={stats['relationships']}。输入 UPDATE 继续：")
            if answer.strip() != "UPDATE": raise SystemExit("已取消，Neo4j 未修改")
        import_dir=Path(config["import_dir"])
        if not import_dir.is_dir(): raise SystemExit(f"Neo4j import_dir not found: {import_dir}")
        for name in ("role_normalization_roles.csv","role_normalization_jobs.csv","role_normalization_edges.csv"):
            shutil.copy2(a.output/name, import_dir/name)
        client=Neo4jHttpClient(config["http_uri"],config.get("database","neo4j"),config.get("username","neo4j"),config["password"])
        if not client.query("RETURN 1 AS ready", access_mode="Read"):
            raise SystemExit("Neo4j preflight returned no result; import cancelled")
        print(f"PREFLIGHT_OK roles={stats['roles']} jobs={stats['jobs']} relationships={stats['relationships']}")
        for statement in cypher_statements(cypher(a.include_ai)):
            client.query(statement, access_mode="Write")
        print("EXECUTE_COMPLETE")
    print(json.dumps(stats,ensure_ascii=False)); return 0

if __name__ == '__main__': sys.exit(main())
