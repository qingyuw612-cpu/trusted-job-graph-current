"""Publish standardized roles using existing cleaned JD abilities. No re-extraction/clustering."""
from __future__ import annotations

import argparse, csv, json, sys, time
from datetime import datetime, timezone
from pathlib import Path

HERE=Path(__file__).resolve().parent; ROOT=HERE.parent; WORKSPACE=ROOT.parent
sys.path.insert(0,str(ROOT)); sys.path.insert(0,str(HERE))
from neo4j_prepare_import import prepare
from trusted_graph_agent.neo4j_repository import Neo4jHttpClient

RESULTS=WORKSPACE/'2026数据51job'/'岗位概念标准化结果'
AUDIT=RESULTS/'neo4j_role_skill_publish'

def chunks(rows,n=300):
    for i in range(0,len(rows),n): yield rows[i:i+n]

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--execute',action='store_true'); ap.add_argument('--yes',action='store_true'); ap.add_argument('--config',type=Path,default=ROOT/'config'/'neo4j_connection.json'); a=ap.parse_args()
    role_rows,_,edges,_,stats=prepare(include_ai=True)
    with (HERE/'role_family_classification.csv').open(encoding='utf-8-sig',newline='') as f:
        families={row['role_id']:row for row in csv.DictReader(f)}
    if set(families) != {row['role_id'] for row in role_rows}:
        raise SystemExit('role_family_classification.csv does not exactly cover role master')
    for row in role_rows:
        row['family_id']=families[row['role_id']]['family_id']
        row['family_name']=families[row['role_id']]['family_name']
    names={r['role_id']:r['name'] for r in role_rows}
    assignments=[{**e,'role_name':names[e['role_id']]} for e in edges]
    cfg=json.loads(a.config.read_text(encoding='utf-8')); client=Neo4jHttpClient(cfg['http_uri'],cfg.get('database','neo4j'),cfg.get('username','neo4j'),cfg['password'],timeout=300)
    q=lambda s,p=None,w=False: client.query(s,p,access_mode='Write' if w else 'Read')
    active=q("OPTIONAL MATCH (:NormalizationPointer {name:'core'})-[:ACTIVE]->(r:NormalizationRun) RETURN r.run_id AS run_id")[0].get('run_id')
    if not active: raise SystemExit('No active NormalizationRun; cancelled')
    if not a.execute:
        print(json.dumps({'mode':'DRY_RUN','assignments':len(assignments),'roles':len(role_rows),'source_run':active},ensure_ascii=False,indent=2)); return
    if not a.yes and input(f"将复用已清洗能力，发布 {len(role_rows)} 个岗位/{len(assignments)} 条JD。输入 PUBLISH：").strip()!='PUBLISH': raise SystemExit('Cancelled')
    q("CREATE INDEX raw_job_source_job_id IF NOT EXISTS FOR (n:RawJob) ON (n.source_job_id)",w=True)
    q("CREATE INDEX raw_version_standard_role_id IF NOT EXISTS FOR (n:RawJDVersion) ON (n.standard_role_id)",w=True)
    for _ in range(60):
        states=q("SHOW INDEXES YIELD name,state WHERE name IN ['raw_job_source_job_id','raw_version_standard_role_id'] RETURN collect(state) AS states")[0]['states']
        if len(states)==2 and all(x=='ONLINE' for x in states): break
        time.sleep(2)
    else: raise SystemExit('Required indexes did not become ONLINE')
    AUDIT.mkdir(parents=True,exist_ok=True)
    backup=[]; matched=completed=0
    for batch in chunks(assignments):
        rows=q("""UNWIND $rows AS row MATCH (job:RawJob {source_job_id:row.jd_id})-[:CURRENT_VERSION]->(raw:RawJDVersion)
        OPTIONAL MATCH (raw)-[:HAS_PROCESSING_RESULT]->(p:ProcessedJD {status:'COMPLETED'})
        RETURN job.source_job_id AS jd_id,raw.version_id AS version_id,raw.domain_role AS domain_role,
        raw.domain_role_id AS domain_role_id,raw.standard_role_id AS standard_role_id,
        raw.standard_role_name AS standard_role_name,p IS NOT NULL AS processed""",{'rows':batch})
        backup.extend(rows); matched+=len(rows); completed+=sum(bool(x['processed']) for x in rows)
    if matched!=len(assignments): raise SystemExit(f'Preflight mismatch: expected={len(assignments)} matched={matched}')
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'); run_id=f'role_standardization:{stamp}'
    (AUDIT/f'backup_{stamp}.json').write_text(json.dumps({'source_run':active,'new_run':run_id,'stats':stats,'raw_versions':backup},ensure_ascii=False,indent=2),encoding='utf-8')
    for batch in chunks(assignments):
        q("""UNWIND $rows AS row MATCH (job:RawJob {source_job_id:row.jd_id})-[:CURRENT_VERSION]->(raw:RawJDVersion)
        SET raw.domain_role=row.role_name,raw.domain_role_id=row.role_id,
            raw.standard_role_id=row.role_id,raw.standard_role_name=row.role_name,
            raw.role_approval_status=row.approval_status,raw.role_confidence=toFloat(row.confidence),
            raw.role_definition_version=row.definition_version,raw.role_source_batch=row.source_batch""",{'rows':batch},True)
    q("""MERGE (run:NormalizationRun {run_id:$run}) SET run.status='STAGING',run.created_at=$now,
       run.source_run_id=$source,run.expected_roles=$roles,run.expected_jds=$jds,run.mode='ROLE_STANDARDIZATION_REAGGREGATION'""",
      {'run':run_id,'now':datetime.now(timezone.utc).isoformat(),'source':active,'roles':len(role_rows),'jds':len(assignments)},True)
    for batch in chunks(role_rows):
        q("""UNWIND $rows AS row MERGE (d:Domain {domain_id:'it'}) SET d.name='信息技术'
        MERGE (f:RoleFamily {family_id:row.family_id}) SET f.name=row.family_name,f.domain_id='it',f.domain_name='信息技术'
        MERGE (d)-[:HAS_FAMILY]->(f) MERGE (r:Role {role_id:row.role_id})
        SET r.name=row.name,r.role_name=row.name,r.family_id=row.family_id,r.family_name=row.family_name,
            r.domain_id='it',r.domain_name='信息技术',r.approval_status=row.approval_status,
            r.source_batch=row.source_batch,r.definition_version=row.definition_version,r.normalization_run_id=$run
        WITH row,f,r OPTIONAL MATCH (old:RoleFamily)-[e:HAS_ROLE]->(r) WHERE old.family_id<>row.family_id DELETE e
        MERGE (f)-[:HAS_ROLE]->(r)""",{'rows':batch,'run':run_id},True)
    q("""MATCH (a:AbilityCandidate)-[old:NORMALIZES_TO {run_id:$source}]->(s:NormalizedSkill)
    MERGE (a)-[e:NORMALIZES_TO {run_id:$run}]->(s) SET e.published_at=$now,e.source_run_id=$source""",
      {'source':active,'run':run_id,'now':datetime.now(timezone.utc).isoformat()},True)
    published_roles=core_edges=0
    for role in role_rows:
        total_row=q("""MATCH (raw:RawJDVersion {standard_role_id:$rid})-[:HAS_PROCESSING_RESULT]->(p:ProcessedJD {status:'COMPLETED'})
        RETURN count(DISTINCT raw.version_id) AS n,count(DISTINCT raw.company_id) AS companies""",{'rid':role['role_id']})[0]
        total=int(total_row['n'] or 0)
        if not total: continue
        skills=q("""MATCH (raw:RawJDVersion {standard_role_id:$rid})-[:HAS_PROCESSING_RESULT]->(p:ProcessedJD {status:'COMPLETED'})
        -[m:HAS_ABILITY]->(a:AbilityCandidate)-[:NORMALIZES_TO {run_id:$run}]->(s:NormalizedSkill)
        WHERE m.evidence_status='VERIFIED'
        RETURN s.concept_id AS concept_id,count(DISTINCT raw.version_id) AS jd_count,
        count(DISTINCT raw.company_id) AS company_count,count(m) AS evidence_count
        ORDER BY jd_count DESC,company_count DESC,evidence_count DESC LIMIT 30""",{'rid':role['role_id'],'run':run_id})
        rows=[]
        for rank,x in enumerate(skills,1):
            if int(x['jd_count']) < (3 if total>=20 else 1): continue
            rows.append({**x,'role_id':role['role_id'],'run_id':run_id,'rank':rank,'score':int(x['jd_count'])/total})
        if rows:
            q("""UNWIND $rows AS row MATCH (r:Role {role_id:row.role_id}),(s:NormalizedSkill {concept_id:row.concept_id})
            MERGE (r)-[e:HAS_CORE_SKILL {run_id:row.run_id}]->(s)
            SET e.final_score=row.score,e.jd_count=row.jd_count,e.company_count=row.company_count,
                e.verified_jd_count=row.jd_count,e.evidence_count=row.evidence_count,e.rank=row.rank,e.published_at=$now""",
              {'rows':rows,'now':datetime.now(timezone.utc).isoformat()},True)
            published_roles+=1; core_edges+=len(rows)
        q("MATCH (r:Role {role_id:$rid}) SET r.document_count=$docs,r.company_count=$companies",{'rid':role['role_id'],'docs':total,'companies':int(total_row['companies'] or 0)},True)
        print(f"aggregated {published_roles}/{len(role_rows)} {role['name']} skills={len(rows)}",flush=True)
    mapped=q("MATCH (:AbilityCandidate)-[e:NORMALIZES_TO {run_id:$run}]->(:NormalizedSkill) RETURN count(e) AS n",{'run':run_id})[0]['n']
    if published_roles<1 or core_edges<1 or int(mapped)<1: raise SystemExit('Publish verification failed; pointer unchanged')
    q("""MATCH (run:NormalizationRun {run_id:$run}) MERGE (ptr:NormalizationPointer {name:'core'})
    OPTIONAL MATCH (ptr)-[old:ACTIVE]->(prev:NormalizationRun) DELETE old
    WITH ptr,run,collect(prev) AS previous MERGE (ptr)-[:ACTIVE]->(run)
    SET run.status='ACTIVE',run.activated_at=$now,run.actual_roles=$roles,run.actual_core_edges=$edges,run.mapped_edges=$mapped
    FOREACH (p IN previous | SET p.status=CASE WHEN p.run_id=$run THEN 'ACTIVE' ELSE 'ARCHIVED' END)""",
      {'run':run_id,'now':datetime.now(timezone.utc).isoformat(),'roles':published_roles,'edges':core_edges,'mapped':int(mapped)},True)
    removed=q("MATCH (j:JD {source_batch:'job_role_mapping_draft'}) WITH collect(j) AS nodes,size(collect(j)) AS n FOREACH (x IN nodes | DETACH DELETE x) RETURN n",w=True)[0]['n']
    result={'status':'ACTIVE','run_id':run_id,'previous_run':active,'roles_with_skills':published_roles,'core_edges':core_edges,'mapped_abilities':int(mapped),'raw_matched':matched,'processed_completed':completed,'removed_lightweight_jds':int(removed),'audit':str(AUDIT/f'backup_{stamp}.json')}
    (AUDIT/f'result_{stamp}.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8'); print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
