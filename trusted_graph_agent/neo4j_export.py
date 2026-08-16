from __future__ import annotations

import csv
from pathlib import Path

from .models import GraphBundle


CSV_LAYOUT = {
    "role_families.csv": (
        "role_families", ["family_id", "family_name", "domain_id", "domain_name"],
    ),
    "role_aliases.csv": (
        "role_aliases", ["alias_id", "role_id", "role_name", "alias"],
    ),
    "role_relations.csv": (
        "role_relations", ["relation_id", "parent_role_id", "child_role_id", "relation"],
    ),
    "roles.csv": (
        "roles",
        [
            "role_id", "role_name", "parent_role_id", "parent_role_name",
            "family_id", "family_name", "domain_id", "domain_name",
            "document_count", "company_count", "industries",
        ],
    ),
    "role_profiles.csv": (
        "role_profiles",
        [
            "profile_id", "role_id", "role_name", "industry_id", "industry_name", "level_id",
            "level_name", "window_id", "time_window", "window_start", "jd_count", "company_count", "previous_profile_id",
        ],
    ),
    "skills.csv": (
        "skills",
        ["skill_id", "canonical_name", "aliases", "competency_category", "tech_stack", "registry_version"],
    ),
    "industries.csv": ("industries", ["industry_id", "industry_name"]),
    "levels.csv": ("levels", ["level_id", "level_name"]),
    "time_windows.csv": ("time_windows", ["window_id", "time_window", "window_start"]),
    "companies.csv": ("companies", ["company_id", "source_company_id", "company_name"]),
    "jds.csv": (
        "jds",
        [
            "jd_id", "raw_job_id", "title", "canonical_role", "role_id", "profile_id", "company_id",
            "company_name", "industry_id", "industry_name", "industry_detail", "level_id", "level_name",
            "education", "experience", "salary", "location", "posted_at", "source_file", "description",
            "tags", "duplicate_of", "duplicate_reason", "template_cluster_id", "template_weight", "time_weight",
            "base_weight",
        ],
    ),
    "role_skill_edges.csv": (
        "role_skill_edges",
        [
            "edge_id", "profile_id", "role_id", "skill_id", "relation", "tier", "jd_support",
            "company_support", "adjusted_support", "company_count", "effective_company_count",
            "evidence_count", "preferred_mentions",
        ],
    ),
    "role_skill_snapshots.csv": (
        "role_skill_snapshots",
        [
            "snapshot_id", "role_id", "skill_id", "time_window", "window_start", "relation", "tier",
            "adjusted_support", "jd_support", "company_support", "evidence_count", "jd_count",
            "company_count", "previous_support", "delta", "trend",
        ],
    ),
    "jd_skill_edges.csv": (
        "jd_skill_edges",
        [
            "edge_id", "jd_id", "profile_id", "skill_id", "skill_name", "raw_term", "requirement_type",
            "evidence_quote", "evidence_status", "confidence", "source", "competency_category", "tech_stack",
        ],
    ),
    "skill_related_edges.csv": (
        "related_skill_edges",
        ["edge_id", "source_skill_id", "target_skill_id", "relation", "cooccurrence", "jaccard_score"],
    ),
    "evolution_edges.csv": (
        "evolution_edges",
        [
            "evolution_id", "previous_profile_id", "current_profile_id", "skill_id", "change_type",
            "previous_support", "current_support", "delta",
        ],
    ),
    "review_tasks.csv": (
        "review_tasks",
        [
            "task_id", "jd_id", "skill_id", "skill_name", "reason", "evidence_status", "confidence",
            "evidence_quote", "status", "decision",
        ],
    ),
}


CONSTRAINTS = """// Neo4j 5.x 约束与索引
CREATE CONSTRAINT role_id IF NOT EXISTS FOR (n:Role) REQUIRE n.role_id IS UNIQUE;
CREATE CONSTRAINT role_family_id IF NOT EXISTS FOR (n:RoleFamily) REQUIRE n.family_id IS UNIQUE;
CREATE CONSTRAINT role_alias_id IF NOT EXISTS FOR (n:RoleAlias) REQUIRE n.alias_id IS UNIQUE;
CREATE CONSTRAINT skill_snapshot_id IF NOT EXISTS FOR (n:SkillSnapshot) REQUIRE n.snapshot_id IS UNIQUE;
CREATE CONSTRAINT role_profile_id IF NOT EXISTS FOR (n:RoleProfile) REQUIRE n.profile_id IS UNIQUE;
CREATE CONSTRAINT skill_id IF NOT EXISTS FOR (n:Skill) REQUIRE n.skill_id IS UNIQUE;
CREATE CONSTRAINT industry_id IF NOT EXISTS FOR (n:Industry) REQUIRE n.industry_id IS UNIQUE;
CREATE CONSTRAINT level_id IF NOT EXISTS FOR (n:Level) REQUIRE n.level_id IS UNIQUE;
CREATE CONSTRAINT time_window_id IF NOT EXISTS FOR (n:TimeWindow) REQUIRE n.window_id IS UNIQUE;
CREATE CONSTRAINT company_id IF NOT EXISTS FOR (n:Company) REQUIRE n.company_id IS UNIQUE;
CREATE CONSTRAINT jd_id IF NOT EXISTS FOR (n:JD) REQUIRE n.jd_id IS UNIQUE;
CREATE INDEX role_name IF NOT EXISTS FOR (n:Role) ON (n.name);
CREATE INDEX skill_name IF NOT EXISTS FOR (n:Skill) ON (n.name);
CREATE INDEX profile_window IF NOT EXISTS FOR (n:RoleProfile) ON (n.time_window);
"""


IMPORT_CYPHER = """// 将本目录 CSV 复制到 Neo4j import 目录后执行。
LOAD CSV WITH HEADERS FROM 'file:///role_families.csv' AS row
MERGE (d:Domain {domain_id: row.domain_id}) SET d.name = row.domain_name
MERGE (f:RoleFamily {family_id: row.family_id})
SET f.name = row.family_name, f.domain_id = row.domain_id, f.domain_name = row.domain_name
MERGE (d)-[:HAS_FAMILY]->(f);

LOAD CSV WITH HEADERS FROM 'file:///roles.csv' AS row
MERGE (n:Role {role_id: row.role_id})
SET n.name = row.role_name, n.document_count = toInteger(row.document_count),
    n.company_count = toInteger(row.company_count), n.industries = row.industries,
    n.parent_role_name = row.parent_role_name, n.family_id = row.family_id,
    n.family_name = row.family_name, n.domain_id = row.domain_id, n.domain_name = row.domain_name;

LOAD CSV WITH HEADERS FROM 'file:///roles.csv' AS row
WITH row WHERE row.family_id <> ''
MATCH (r:Role {role_id: row.role_id}), (f:RoleFamily {family_id: row.family_id})
MERGE (f)-[:HAS_ROLE]->(r);

LOAD CSV WITH HEADERS FROM 'file:///role_aliases.csv' AS row
MATCH (r:Role {role_id: row.role_id})
MERGE (a:RoleAlias {alias_id: row.alias_id}) SET a.name = row.alias
MERGE (a)-[:ALIAS_OF]->(r);

LOAD CSV WITH HEADERS FROM 'file:///roles.csv' AS row
WITH row WHERE row.parent_role_id <> ''
MATCH (child:Role {role_id: row.role_id}), (parent:Role {role_id: row.parent_role_id})
MERGE (child)-[:SUBTYPE_OF]->(parent);

LOAD CSV WITH HEADERS FROM 'file:///industries.csv' AS row
MERGE (n:Industry {industry_id: row.industry_id}) SET n.name = row.industry_name;

LOAD CSV WITH HEADERS FROM 'file:///levels.csv' AS row
MERGE (n:Level {level_id: row.level_id}) SET n.name = row.level_name;

LOAD CSV WITH HEADERS FROM 'file:///time_windows.csv' AS row
MERGE (n:TimeWindow {window_id: row.window_id})
SET n.name = row.time_window, n.window_start = row.window_start;

LOAD CSV WITH HEADERS FROM 'file:///skills.csv' AS row
MERGE (n:Skill {skill_id: row.skill_id})
SET n.name = row.canonical_name, n.aliases = row.aliases,
    n.competency_category = row.competency_category, n.tech_stack = row.tech_stack,
    n.registry_version = row.registry_version;

LOAD CSV WITH HEADERS FROM 'file:///companies.csv' AS row
MERGE (n:Company {company_id: row.company_id})
SET n.name = row.company_name, n.source_company_id = row.source_company_id;

LOAD CSV WITH HEADERS FROM 'file:///role_profiles.csv' AS row
MERGE (p:RoleProfile {profile_id: row.profile_id})
SET p.time_window = row.time_window, p.window_start = row.window_start,
    p.role_name = row.role_name, p.industry_name = row.industry_name, p.level_name = row.level_name,
    p.jd_count = toInteger(row.jd_count), p.company_count = toInteger(row.company_count)
WITH row, p
MATCH (r:Role {role_id: row.role_id}), (i:Industry {industry_id: row.industry_id}),
      (l:Level {level_id: row.level_id}), (w:TimeWindow {window_id: row.window_id})
MERGE (r)-[:HAS_PROFILE]->(p)
MERGE (p)-[:IN_INDUSTRY]->(i)
MERGE (p)-[:AT_LEVEL]->(l)
MERGE (p)-[:IN_WINDOW]->(w);

LOAD CSV WITH HEADERS FROM 'file:///role_profiles.csv' AS row
WITH row WHERE row.previous_profile_id <> ''
MATCH (current:RoleProfile {profile_id: row.profile_id})
MATCH (previous:RoleProfile {profile_id: row.previous_profile_id})
MERGE (current)-[:PREVIOUS_VERSION]->(previous);

LOAD CSV WITH HEADERS FROM 'file:///jds.csv' AS row
MERGE (j:JD {jd_id: row.jd_id})
SET j.title = row.title, j.posted_at = row.posted_at, j.source_file = row.source_file,
    j.education = row.education, j.experience = row.experience, j.salary = row.salary,
    j.location = row.location, j.description = row.description, j.tags = row.tags,
    j.profile_id = row.profile_id, j.company_name = row.company_name,
    j.duplicate_of = row.duplicate_of,
    j.duplicate_reason = row.duplicate_reason, j.template_cluster_id = row.template_cluster_id,
    j.template_weight = toFloat(row.template_weight), j.time_weight = toFloat(row.time_weight)
WITH row, j
MATCH (r:Role {role_id: row.role_id}), (c:Company {company_id: row.company_id})
MERGE (j)-[:INSTANCE_OF]->(r)
MERGE (j)-[:POSTED_BY]->(c)
WITH row, j
MATCH (p:RoleProfile {profile_id: row.profile_id})
MERGE (j)-[:SUPPORTS_PROFILE]->(p);

LOAD CSV WITH HEADERS FROM 'file:///role_skill_edges.csv' AS row
WITH row WHERE row.relation = 'REQUIRES_SKILL'
MATCH (p:RoleProfile {profile_id: row.profile_id}), (s:Skill {skill_id: row.skill_id})
MERGE (p)-[r:REQUIRES_SKILL]->(s)
SET r.tier = row.tier, r.jd_support = toFloat(row.jd_support),
    r.company_support = toFloat(row.company_support), r.adjusted_support = toFloat(row.adjusted_support),
    r.company_count = toInteger(row.company_count),
    r.effective_company_count = toFloat(row.effective_company_count),
    r.evidence_count = toInteger(row.evidence_count);

LOAD CSV WITH HEADERS FROM 'file:///role_skill_edges.csv' AS row
WITH row WHERE row.relation = 'PREFERS_SKILL'
MATCH (p:RoleProfile {profile_id: row.profile_id}), (s:Skill {skill_id: row.skill_id})
MERGE (p)-[r:PREFERS_SKILL]->(s)
SET r.tier = row.tier, r.jd_support = toFloat(row.jd_support),
    r.company_support = toFloat(row.company_support), r.adjusted_support = toFloat(row.adjusted_support),
    r.company_count = toInteger(row.company_count), r.evidence_count = toInteger(row.evidence_count);

LOAD CSV WITH HEADERS FROM 'file:///jd_skill_edges.csv' AS row
WITH row WHERE row.evidence_status = 'VERIFIED'
MATCH (j:JD {jd_id: row.jd_id}), (s:Skill {skill_id: row.skill_id})
MERGE (j)-[r:MENTIONS_SKILL]->(s)
SET r.raw_term = row.raw_term, r.requirement_type = row.requirement_type,
    r.evidence_quote = row.evidence_quote, r.evidence_status = row.evidence_status,
    r.confidence = toFloat(row.confidence), r.source = row.source,
    r.skill_id = row.skill_id, r.skill_name = row.skill_name;

LOAD CSV WITH HEADERS FROM 'file:///skill_related_edges.csv' AS row
MATCH (a:Skill {skill_id: row.source_skill_id}), (b:Skill {skill_id: row.target_skill_id})
MERGE (a)-[r:RELATED_TO]->(b)
SET r.cooccurrence = toInteger(row.cooccurrence), r.jaccard_score = toFloat(row.jaccard_score);

LOAD CSV WITH HEADERS FROM 'file:///evolution_edges.csv' AS row
MATCH (current:RoleProfile {profile_id: row.current_profile_id}), (s:Skill {skill_id: row.skill_id})
MERGE (current)-[r:SKILL_EVOLUTION {evolution_id: row.evolution_id}]->(s)
SET r.change_type = row.change_type, r.previous_support = toFloat(row.previous_support),
    r.current_support = toFloat(row.current_support), r.delta = toFloat(row.delta),
    r.previous_profile_id = row.previous_profile_id;

LOAD CSV WITH HEADERS FROM 'file:///role_skill_snapshots.csv' AS row
MATCH (role:Role {role_id: row.role_id}), (skill:Skill {skill_id: row.skill_id})
MERGE (snapshot:SkillSnapshot {snapshot_id: row.snapshot_id})
SET snapshot.time_window = row.time_window, snapshot.window_start = row.window_start,
    snapshot.support = toFloat(row.adjusted_support), snapshot.delta = toFloat(row.delta),
    snapshot.previous_support = toFloat(row.previous_support), snapshot.trend = row.trend,
    snapshot.tier = row.tier, snapshot.relation = row.relation,
    snapshot.evidence_count = toInteger(row.evidence_count)
MERGE (role)-[:HAS_SKILL_SNAPSHOT]->(snapshot)
MERGE (snapshot)-[:OF_SKILL]->(skill);
"""


README = """# Neo4j 导入目录

1. 安装并启动 Neo4j 5.x，或启动官方 Docker 镜像。
2. 将本目录所有 CSV 文件复制到 Neo4j 配置的 `import` 目录。
3. 在 Neo4j Browser 先执行 `constraints.cypher`，再执行 `import.cypher`。
4. 脚本全部使用 `MERGE`，同一批次可重复执行，不会重复创建主实体。

快速验收查询：

```cypher
MATCH (r:Role)-[:HAS_PROFILE]->(p:RoleProfile)-[e:REQUIRES_SKILL]->(s:Skill)
RETURN r.name, p.time_window, s.name, e.tier, e.adjusted_support
ORDER BY e.adjusted_support DESC LIMIT 30;
```
"""


def export_neo4j_stage(bundle: GraphBundle, output_dir: Path) -> Path:
    target = output_dir / "neo4j"
    target.mkdir(parents=True, exist_ok=True)
    for filename, (attribute, fields) in CSV_LAYOUT.items():
        rows = getattr(bundle, attribute)
        with (target / filename).open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    (target / "constraints.cypher").write_text(CONSTRAINTS, encoding="utf-8")
    (target / "import.cypher").write_text(IMPORT_CYPHER, encoding="utf-8")
    (target / "README.md").write_text(README, encoding="utf-8")
    return target
