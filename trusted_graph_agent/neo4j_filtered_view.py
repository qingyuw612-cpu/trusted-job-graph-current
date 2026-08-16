from __future__ import annotations

from typing import Any


LEVEL_ORDER = {
    "实习/应届": 1,
    "初级": 2,
    "中级": 3,
    "高级": 4,
    "专家": 5,
    "管理岗": 6,
    "未注明": 7,
}


def _active_normalization_run(client: Any) -> str:
    rows = client.query(
        """
        OPTIONAL MATCH (:NormalizationPointer {name:'core'})-[:ACTIVE]->(run:NormalizationRun)
        RETURN run.run_id AS run_id
        """
    )
    return str(rows[0].get("run_id") or "") if rows else ""


def load_facets(client: Any) -> dict:
    active_run_id = _active_normalization_run(client)
    industries = client.query(
        "MATCH (n:Industry) RETURN n.industry_id AS industry_id, n.name AS industry_name ORDER BY n.name"
    )
    levels = client.query(
        "MATCH (:RoleProfile)-[:AT_LEVEL]->(n:Level) "
        "RETURN DISTINCT n.level_id AS level_id, n.name AS level_name"
    )
    levels.sort(key=lambda row: (LEVEL_ORDER.get(row.get("level_name") or "", 99), row.get("level_name") or ""))
    stacks = [
        row["tech_stack"]
        for row in client.query(
            "MATCH (s:Skill) WHERE coalesce(s.tech_stack, '') <> '' "
            "RETURN DISTINCT s.tech_stack AS tech_stack ORDER BY tech_stack"
        )
    ]
    families = client.query(
        "MATCH (f:RoleFamily)-[:HAS_ROLE]->(r:Role) "
        "WHERE $run_id = '' OR EXISTS { MATCH (r)-[:HAS_CORE_SKILL {run_id:$run_id}]->() } "
        "RETURN DISTINCT f.family_id AS family_id, f.name AS family_name, "
        "f.domain_id AS domain_id, f.domain_name AS domain_name ORDER BY f.name",
        {"run_id": active_run_id},
    )
    windows = [
        row["time_window"]
        for row in client.query(
            "MATCH (p:RoleProfile) WHERE coalesce(p.time_window, '') <> '' "
            "RETURN p.time_window AS time_window, min(p.window_start) AS window_start ORDER BY window_start"
        )
    ]
    if active_run_id:
        categories = [
            row["category"]
            for row in client.query(
                """
                MATCH (:Role)-[:HAS_CORE_SKILL {run_id:$run_id}]->(s:NormalizedSkill)
                WHERE coalesce(s.category, '') <> ''
                RETURN DISTINCT s.category AS category ORDER BY category
                """,
                {"run_id": active_run_id},
            )
        ]
        windows = ["全量", *windows]
    else:
        categories = [
            row["category"]
            for row in client.query(
                "MATCH (s:NormalizedSkill) WHERE coalesce(s.category, '') <> '' "
                "RETURN DISTINCT s.category AS category ORDER BY category"
            )
        ]
    return {
        "industries": industries,
        "levels": levels,
        "stacks": stacks,
        "categories": categories,
        "time_windows": windows,
        "families": families,
    }


def load_roles(client: Any, level: str = "") -> list[dict]:
    active_run_id = _active_normalization_run(client)
    if active_run_id and not level:
        return client.query(
            """
            MATCH (r:Role)-[:HAS_CORE_SKILL {run_id:$run_id}]->(:NormalizedSkill)
            WITH DISTINCT r
            OPTIONAL MATCH (family:RoleFamily)-[:HAS_ROLE]->(r)
            WITH r, head(collect(family)) AS family
            RETURN r.role_id AS role_id, r.name AS role_name,
                   '' AS parent_role_id, '' AS parent_role_name,
                   family.family_id AS family_id, family.name AS family_name,
                   r.document_count AS document_count, r.company_count AS company_count
            ORDER BY document_count DESC, role_name
            """,
            {"run_id": active_run_id},
        )
    return client.query(
        """
        MATCH (r:Role)
        OPTIONAL MATCH (family:RoleFamily)-[:HAS_ROLE]->(r)
        WITH r, head(collect(family)) AS family
        WHERE $level = '' OR EXISTS {
            MATCH (r)-[:HAS_PROFILE]->(:RoleProfile)-[:AT_LEVEL]->(:Level {name:$level})
        }
        RETURN r.role_id AS role_id, r.name AS role_name,
               '' AS parent_role_id, '' AS parent_role_name,
               family.family_id AS family_id, family.name AS family_name,
               r.document_count AS document_count, r.company_count AS company_count
        ORDER BY document_count DESC, role_name
        """,
        {"level": level},
    )


def _default_role_id(client: Any) -> str:
    rows = client.query(
        "MATCH (r:Role) RETURN r.role_id AS role_id "
        "ORDER BY CASE r.name WHEN '产品经理' THEN 0 ELSE 1 END, r.document_count DESC LIMIT 1"
    )
    return rows[0]["role_id"] if rows else ""


def _role_windows(client: Any, role_id: str, level: str) -> list[dict]:
    return client.query(
        """
        MATCH (:Role {role_id:$role_id})-[:HAS_PROFILE]->(p:RoleProfile)
        MATCH (p)-[:AT_LEVEL]->(l:Level)
        WHERE $level = '' OR l.name = $level
        RETURN p.time_window AS time_window, min(p.window_start) AS window_start
        ORDER BY window_start
        """,
        {"role_id": role_id, "level": level},
    )


def _published_role_windows(client: Any, role_id: str) -> list[str]:
    rows = client.query(
        """
        CALL {
            MATCH (raw:RawJDVersion {standard_role_id:$role_id})
                  -[:HAS_PROCESSING_RESULT]->(:ProcessedJD {status:'COMPLETED'})
            USING INDEX raw:RawJDVersion(standard_role_id)
            WITH split(replace(coalesce(raw.publish_time_raw, ''), '-', '/'), '/') AS parts
            WHERE size(parts) >= 2
              AND toInteger(parts[0]) IS NOT NULL
              AND toInteger(parts[1]) >= 1 AND toInteger(parts[1]) <= 12
            RETURN DISTINCT parts[0] + 'Q' +
                   toString(toInteger((toInteger(parts[1]) - 1) / 3) + 1) AS time_window
            UNION
            MATCH (:Role {role_id:$role_id})-[snapshot:HAS_SKILL_SNAPSHOT]->(:NormalizedSkill)
            WHERE coalesce(snapshot.time_window, '') <> ''
            RETURN DISTINCT snapshot.time_window AS time_window
        }
        RETURN DISTINCT time_window
        ORDER BY time_window
        """,
        {"role_id": role_id},
    )
    return [str(row.get("time_window") or "") for row in rows if row.get("time_window")]


def _published_panorama(
    client: Any,
    role: dict,
    run_id: str,
    category: str,
    min_support: float,
    skill_limit: int,
    available_windows: list[str] | None = None,
) -> dict | None:
    role_id = role["role_id"]
    skills = client.query(
        """
        MATCH (:Role {role_id:$role_id})
              -[edge:HAS_CORE_SKILL {run_id:$run_id}]->(skill:NormalizedSkill)
        WHERE edge.final_score >= $min_support
          AND ($category = '' OR skill.category = $category)
        RETURN skill.concept_id AS skill_id,
               skill.canonical_name AS canonical_name,
               skill.category AS competency_category,
               edge.final_score AS adjusted_support,
               edge.verified_jd_count AS evidence_count,
               edge.rank AS skill_rank
        ORDER BY skill_rank
        LIMIT $skill_limit
        """,
        {
            "role_id": role_id,
            "run_id": run_id,
            "category": category,
            "min_support": max(0.0, min(float(min_support), 1.0)),
            "skill_limit": max(1, min(int(skill_limit), 50)),
        },
    )
    if not skills:
        return None

    nodes: list[dict] = []
    edges: list[dict] = []
    family_id = role.get("family_id") or ""
    nodes.append(
        {
            "id": role_id,
            "entity_id": role_id,
            "type": "role",
            "label": role["role_name"],
            "family_id": family_id,
            "jd_count": role.get("document_count") or 0,
            "company_count": role.get("company_count") or 0,
            "focused": True,
        }
    )
    category_ids: set[str] = set()
    for row in skills:
        category_name = row.get("competency_category") or "其他能力"
        category_id = f"category:{role_id}:{category_name}"
        if category_id not in category_ids:
            nodes.append(
                {
                    "id": category_id,
                    "entity_id": category_name,
                    "role_id": role_id,
                    "type": "category",
                    "label": category_name,
                }
            )
            edges.append(
                {
                    "id": f"role-category:{role_id}:{category_name}",
                    "source": role_id,
                    "target": category_id,
                    "relation": "HAS_SKILL_GROUP",
                }
            )
            category_ids.add(category_id)
        visual_skill_id = f"skill:{role_id}:{row['skill_id']}"
        nodes.append(
            {
                "id": visual_skill_id,
                "entity_id": row["skill_id"],
                "role_id": role_id,
                "type": "skill",
                "label": row["canonical_name"],
                "stack": "",
                "category": category_name,
                "support": row.get("adjusted_support") or 0,
                "tier": "core",
                "trend": "STABLE",
                "delta": 0,
                "evidence_count": row.get("evidence_count") or 0,
                "time_window": "全量",
            }
        )
        edges.append(
            {
                "id": f"category-skill:{role_id}:{row['skill_id']}",
                "source": category_id,
                "target": visual_skill_id,
                "relation": "HAS_CORE_SKILL",
                "support": row.get("adjusted_support") or 0,
            }
        )
    return {
        "nodes": nodes,
        "edges": edges,
        "related_edges": [],
        "stats": {
            "roles": 1,
            "skills": len(skills),
            "categories": len(category_ids),
            "edges": len(edges),
            "evidence": sum(int(row.get("evidence_count") or 0) for row in skills),
            "filtered_jds": int(role.get("document_count") or 0),
        },
        "quality": {
            "backend": "neo4j",
            "normalized_graph": True,
            "published_summary": True,
            "normalization_run_id": run_id,
        },
        "available_windows": available_windows or [],
        "filters": {
            "role_id": role_id,
            "category": category,
            "effective_time_window": "全量",
            "min_support": min_support,
            "skill_limit": skill_limit,
        },
    }


def _historical_snapshot_panorama(
    client: Any,
    role: dict,
    time_window: str,
    category: str,
    min_support: float,
    skill_limit: int,
    available_windows: list[str],
) -> dict:
    parameters = {
        "role_id": role["role_id"], "time_window": time_window,
        "category": category, "min_support": max(0.0, min(float(min_support), 1.0)),
        "skill_limit": max(1, min(int(skill_limit), 50)),
    }
    rows = client.query(
        """
        MATCH (:Role {role_id:$role_id})-[e:HAS_SKILL_SNAPSHOT {time_window:$time_window}]
              ->(s:NormalizedSkill)
        WHERE e.final_score >= $min_support AND ($category='' OR s.category=$category)
        RETURN s.concept_id AS skill_id,s.canonical_name AS canonical_name,
               s.category AS competency_category,e.final_score AS adjusted_support,
               e.verified_jd_count AS evidence_count,e.rank AS skill_rank,
               e.trend AS trend,e.delta AS delta
        ORDER BY skill_rank,adjusted_support DESC LIMIT $skill_limit
        """, parameters,
    )
    total = client.query(
        """MATCH (:Role {role_id:$role_id})-[:HAS_PROFILE]->(p:RoleProfile {time_window:$time_window})
        RETURN sum(coalesce(p.jd_count,0)) AS n""", parameters,
    )
    total_jds = int(total[0].get("n") or 0) if total else 0
    nodes = [{"id":role["role_id"],"entity_id":role["role_id"],"type":"role",
              "label":role["role_name"],"family_id":role.get("family_id") or "",
              "jd_count":total_jds,"company_count":role.get("company_count") or 0,"focused":True}]
    edges=[]; category_ids=set()
    for row in rows:
        category_name=row.get("competency_category") or "其他能力"
        category_id=f"category:{role['role_id']}:{category_name}"
        if category_id not in category_ids:
            nodes.append({"id":category_id,"entity_id":category_name,"role_id":role["role_id"],"type":"category","label":category_name})
            edges.append({"id":f"role-category:{role['role_id']}:{category_name}","source":role["role_id"],"target":category_id,"relation":"HAS_SKILL_GROUP"})
            category_ids.add(category_id)
        skill_id=f"skill:{role['role_id']}:{row['skill_id']}"
        nodes.append({"id":skill_id,"entity_id":row["skill_id"],"role_id":role["role_id"],"type":"skill",
                      "label":row["canonical_name"],"category":category_name,"support":row.get("adjusted_support") or 0,
                      "tier":"historical_snapshot","trend":row.get("trend") or "STABLE","delta":row.get("delta") or 0,
                      "evidence_count":row.get("evidence_count") or 0,"time_window":time_window})
        edges.append({"id":f"category-skill:{role['role_id']}:{row['skill_id']}","source":category_id,"target":skill_id,
                      "relation":"HAS_CORE_SKILL","support":row.get("adjusted_support") or 0})
    return {"nodes":nodes,"edges":edges,"related_edges":[],
            "stats":{"roles":1,"skills":len(rows),"categories":len(category_ids),"edges":len(edges),
                     "evidence":sum(int(x.get("evidence_count") or 0) for x in rows),"filtered_jds":total_jds},
            "quality":{"backend":"neo4j","normalized_graph":True,"historical_snapshot":True},
            "available_windows":available_windows,
            "filters":{"role_id":role["role_id"],"category":category,"time_window":time_window,
                       "effective_time_window":time_window,"min_support":parameters["min_support"],"skill_limit":parameters["skill_limit"]}}


def _published_time_panorama(
    client: Any,
    role: dict,
    run_id: str,
    category: str,
    time_window: str,
    stack: str,
    min_support: float,
    skill_limit: int,
    available_windows: list[str],
) -> dict:
    role_id = role["role_id"]
    role_name = role["role_name"]
    parameters = {
        "role_id": role_id,
        "role_name": role_name,
        "run_id": run_id,
        "category": category,
        "time_window": time_window,
        "stack": stack,
        "min_support": max(0.0, min(float(min_support), 1.0)),
        "skill_limit": max(1, min(int(skill_limit), 50)),
    }
    total_rows = client.query(
        """
        MATCH (raw:RawJDVersion {standard_role_id:$role_id})-[:HAS_PROCESSING_RESULT]->
              (processed:ProcessedJD {status:'COMPLETED'})
        USING INDEX raw:RawJDVersion(standard_role_id)
        WITH raw, processed, split(replace(coalesce(raw.publish_time_raw, ''), '-', '/'), '/') AS parts
        WITH raw, processed,
             CASE WHEN size(parts) >= 2
                  THEN parts[0] + 'Q' +
                       toString(toInteger((toInteger(parts[1]) - 1) / 3) + 1)
                  ELSE '' END AS jd_quarter
        WHERE ($time_window = '' OR jd_quarter = $time_window)
          AND ($stack = '' OR EXISTS {
              MATCH (processed)-[:HAS_ABILITY]->(stackAbility:AbilityCandidate)
              WHERE stackAbility.tech_stack = $stack
          })
        RETURN count(DISTINCT raw.version_id) AS total_jds
        """,
        parameters,
    )
    total_jds = int(total_rows[0].get("total_jds") or 0) if total_rows else 0
    parameters["total_jds"] = max(total_jds, 1)
    skill_rows = client.query(
        """
        MATCH (raw:RawJDVersion {standard_role_id:$role_id})-[:HAS_PROCESSING_RESULT]->
              (processed:ProcessedJD {status:'COMPLETED'})
              -[mention:HAS_ABILITY]->(ability:AbilityCandidate)
              -[:NORMALIZES_TO {run_id:$run_id}]->(skill:NormalizedSkill)
        USING INDEX raw:RawJDVersion(standard_role_id)
        WITH raw, mention, ability, skill,
             split(replace(coalesce(raw.publish_time_raw, ''), '-', '/'), '/') AS parts
        WITH raw, mention, ability, skill,
             CASE WHEN size(parts) >= 2
                  THEN parts[0] + 'Q' +
                       toString(toInteger((toInteger(parts[1]) - 1) / 3) + 1)
                  ELSE '' END AS jd_quarter
        WHERE ($time_window = '' OR jd_quarter = $time_window)
          AND ($stack = '' OR ability.tech_stack = $stack)
          AND ($category = '' OR skill.category = $category)
        WITH skill,
             count(DISTINCT raw.version_id) AS jd_count,
             count(DISTINCT raw.company_id) AS company_count,
             count(mention) AS evidence_count,
             [value IN collect(DISTINCT ability.tech_stack)
              WHERE value IS NOT NULL AND value <> ''] AS stacks
        WITH skill, jd_count, company_count, evidence_count, stacks,
             toFloat(jd_count) / $total_jds AS adjusted_support
        WHERE adjusted_support >= $min_support
        RETURN skill.concept_id AS skill_id,
               skill.canonical_name AS canonical_name,
               skill.category AS competency_category,
               CASE WHEN $stack <> '' THEN $stack ELSE coalesce(head(stacks), '') END AS tech_stack,
               adjusted_support, jd_count, company_count, evidence_count
        ORDER BY adjusted_support DESC, company_count DESC,
                 evidence_count DESC, canonical_name
        LIMIT $skill_limit
        """,
        parameters,
    ) if total_jds else []

    nodes = [{
        "id": role_id,
        "entity_id": role_id,
        "type": "role",
        "label": role_name,
        "family_id": role.get("family_id") or "",
        "jd_count": total_jds,
        "company_count": role.get("company_count") or 0,
        "focused": True,
    }]
    edges: list[dict] = []
    category_ids: set[str] = set()
    effective_window = time_window or "全量"
    for row in skill_rows:
        category_name = row.get("competency_category") or "其他能力"
        category_id = f"category:{role_id}:{category_name}"
        if category_id not in category_ids:
            nodes.append({
                "id": category_id,
                "entity_id": category_name,
                "role_id": role_id,
                "type": "category",
                "label": category_name,
            })
            edges.append({
                "id": f"role-category:{role_id}:{category_name}",
                "source": role_id,
                "target": category_id,
                "relation": "HAS_SKILL_GROUP",
            })
            category_ids.add(category_id)
        visual_skill_id = f"skill:{role_id}:{row['skill_id']}"
        nodes.append({
            "id": visual_skill_id,
            "entity_id": row["skill_id"],
            "role_id": role_id,
            "type": "skill",
            "label": row["canonical_name"],
            "stack": row.get("tech_stack") or "",
            "category": category_name,
            "support": row.get("adjusted_support") or 0,
            "tier": "time_slice",
            "trend": "STABLE",
            "delta": 0,
            "evidence_count": row.get("evidence_count") or 0,
            "time_window": effective_window,
        })
        edges.append({
            "id": f"category-skill:{role_id}:{row['skill_id']}",
            "source": category_id,
            "target": visual_skill_id,
            "relation": "REQUIRES_SKILL",
            "support": row.get("adjusted_support") or 0,
        })
    return {
        "nodes": nodes,
        "edges": edges,
        "related_edges": [],
        "stats": {
            "roles": 1,
            "skills": len(skill_rows),
            "categories": len(category_ids),
            "edges": len(edges),
            "evidence": sum(int(row.get("evidence_count") or 0) for row in skill_rows),
            "filtered_jds": total_jds,
        },
        "quality": {
            "backend": "neo4j",
            "normalized_graph": True,
            "published_time_slice": True,
            "normalization_run_id": run_id,
        },
        "available_windows": available_windows,
        "filters": {
            "role_id": role_id,
            "stack": stack,
            "category": category,
            "time_window": time_window,
            "effective_time_window": effective_window,
            "min_support": parameters["min_support"],
            "skill_limit": parameters["skill_limit"],
        },
    }


def load_panorama(
    client: Any,
    level: str = "",
    stack: str = "",
    category: str = "",
    time_window: str = "",
    role_id: str = "",
    min_support: float = 0.10,
    skill_limit: int = 30,
) -> dict:
    role_id = role_id or _default_role_id(client)
    role_rows = client.query(
        """
        MATCH (r:Role {role_id:$role_id})
        OPTIONAL MATCH (family:RoleFamily)-[:HAS_ROLE]->(r)
        RETURN r.role_id AS role_id, r.name AS role_name,
               head(collect(family.family_id)) AS family_id,
               head(collect(family.name)) AS family_name,
               head(collect(family.domain_id)) AS domain_id,
               head(collect(family.domain_name)) AS domain_name,
               r.document_count AS document_count, r.company_count AS company_count
        """,
        {"role_id": role_id},
    )
    if not role_rows:
        raise KeyError("岗位不存在")

    active_run_id = _active_normalization_run(client)
    if active_run_id and not level:
        available_windows = _published_role_windows(
            client,
            str(role_rows[0].get("role_id") or ""),
        )
        requested_window = "" if time_window in {"", "全量"} else time_window
        if not requested_window and not stack:
            published = _published_panorama(
                client,
                role_rows[0],
                active_run_id,
                category,
                min_support,
                skill_limit,
                available_windows,
            )
            if published is not None:
                return published
        if requested_window:
            historical = client.query(
                """MATCH (:Role {role_id:$role_id})
                          -[e:HAS_SKILL_SNAPSHOT {time_window:$time_window}]->(:NormalizedSkill)
                   RETURN count(e) AS n""",
                {"role_id": role_id, "time_window": requested_window},
            )
            if historical and int(historical[0].get("n") or 0) > 0:
                return _historical_snapshot_panorama(
                    client, role_rows[0], requested_window, category,
                    min_support, skill_limit, available_windows,
                )
        return _published_time_panorama(
            client,
            role_rows[0],
            active_run_id,
            category,
            requested_window,
            stack,
            min_support,
            skill_limit,
            available_windows,
        )

    window_rows = _role_windows(client, role_id, level)
    available_windows = [row["time_window"] for row in window_rows if row.get("time_window")]
    effective_window = time_window or (available_windows[-1] if available_windows else "")
    parameters = {
        "role_id": role_id,
        "level": level,
        "stack": stack,
        "category": category,
        "time_window": effective_window,
        "min_support": max(0.0, min(float(min_support), 1.0)),
        "skill_limit": max(1, min(int(skill_limit), 50)),
    }
    total_rows = client.query(
        """
        MATCH (j:JD)-[:INSTANCE_OF]->(:Role {role_id:$role_id})
        MATCH (j)-[:SUPPORTS_PROFILE]->(p:RoleProfile)
        MATCH (p)-[:AT_LEVEL]->(l:Level)
        WHERE ($level = '' OR l.name = $level)
          AND ($time_window = '' OR p.time_window = $time_window)
          AND coalesce(j.duplicate_of, '') = ''
          AND ($stack = '' OR EXISTS {
              MATCH (j)-[se:MENTIONS_NORMALIZED_SKILL]->(:NormalizedSkill)
              MATCH (raw:Skill {skill_id:se.original_skill_id})
              WHERE raw.tech_stack = $stack
          })
        RETURN count(DISTINCT j) AS total_jds
        """,
        parameters,
    )
    total_jds = int(total_rows[0].get("total_jds") or 0) if total_rows else 0
    parameters["total_jds"] = max(total_jds, 1)
    skill_rows = client.query(
        """
        MATCH (j:JD)-[:INSTANCE_OF]->(r:Role {role_id:$role_id})
        MATCH (j)-[:SUPPORTS_PROFILE]->(p:RoleProfile)
        MATCH (p)-[:AT_LEVEL]->(l:Level)
        MATCH (j)-[e:MENTIONS_NORMALIZED_SKILL]->(skill:NormalizedSkill)
        MATCH (raw:Skill {skill_id:e.original_skill_id})
        WHERE ($level = '' OR l.name = $level)
          AND ($time_window = '' OR p.time_window = $time_window)
          AND coalesce(j.duplicate_of, '') = ''
          AND ($stack = '' OR raw.tech_stack = $stack)
          AND ($category = '' OR skill.category = $category)
        WITH r, skill, count(DISTINCT j) AS jd_count,
             count(DISTINCT e) AS evidence_count,
             count(DISTINCT j.company_name) AS company_count,
             [value IN collect(DISTINCT raw.tech_stack) WHERE value IS NOT NULL AND value <> ''] AS stacks
        WITH r, skill, jd_count, evidence_count, company_count, stacks,
             toFloat(jd_count) / $total_jds AS adjusted_support
        WHERE adjusted_support >= $min_support
        OPTIONAL MATCH (r)-[snapshot:HAS_SKILL_SNAPSHOT {time_window:$time_window}]->(skill)
        RETURN r.role_id AS role_id, skill.concept_id AS skill_id,
               skill.canonical_name AS canonical_name,
               skill.category AS competency_category,
               CASE WHEN $stack <> '' THEN $stack ELSE coalesce(head(stacks), '') END AS tech_stack,
               adjusted_support, jd_count, company_count, evidence_count,
               coalesce(snapshot.trend, 'STABLE') AS trend,
               coalesce(snapshot.delta, 0) AS delta,
               $time_window AS time_window
        ORDER BY adjusted_support DESC, company_count DESC, evidence_count DESC, canonical_name
        LIMIT $skill_limit
        """,
        parameters,
    ) if total_jds else []

    role = role_rows[0]
    nodes = [{
        "id": role_id,
        "entity_id": role_id,
        "type": "role",
        "label": role["role_name"],
        "family_id": role.get("family_id") or "",
        "jd_count": total_jds,
        "company_count": role.get("company_count") or 0,
        "focused": True,
    }]
    edges: list[dict] = []
    category_ids: set[str] = set()
    for row in skill_rows:
        category_name = row.get("competency_category") or "其他能力"
        category_id = f"category:{role_id}:{category_name}"
        if category_id not in category_ids:
            nodes.append({
                "id": category_id,
                "entity_id": category_name,
                "role_id": role_id,
                "type": "category",
                "label": category_name,
            })
            edges.append({
                "id": f"role-category:{role_id}:{category_name}",
                "source": role_id,
                "target": category_id,
                "relation": "HAS_SKILL_GROUP",
            })
            category_ids.add(category_id)
        visual_skill_id = f"skill:{role_id}:{row['skill_id']}"
        nodes.append({
            "id": visual_skill_id,
            "entity_id": row["skill_id"],
            "role_id": role_id,
            "type": "skill",
            "label": row["canonical_name"],
            "stack": row.get("tech_stack") or "",
            "category": category_name,
            "support": row.get("adjusted_support") or 0,
            "tier": "filtered",
            "trend": row.get("trend") or "STABLE",
            "delta": row.get("delta") or 0,
            "evidence_count": row.get("evidence_count") or 0,
            "time_window": effective_window,
        })
        edges.append({
            "id": f"category-skill:{role_id}:{row['skill_id']}",
            "source": category_id,
            "target": visual_skill_id,
            "relation": "REQUIRES_SKILL",
            "support": row.get("adjusted_support") or 0,
        })
    return {
        "nodes": nodes,
        "edges": edges,
        "related_edges": [],
        "stats": {
            "roles": 1,
            "skills": len(skill_rows),
            "categories": len(category_ids),
            "edges": len(edges),
            "evidence": sum(int(row.get("evidence_count") or 0) for row in skill_rows),
            "filtered_jds": total_jds,
        },
        "quality": {"backend": "neo4j", "normalized_graph": True, "filtered_view": True},
        "available_windows": available_windows,
        "filters": {
            "role_id": role_id,
            "level": level,
            "stack": stack,
            "category": category,
            "time_window": time_window,
            "effective_time_window": effective_window,
            "min_support": parameters["min_support"],
            "skill_limit": parameters["skill_limit"],
        },
    }


def load_skill_evidence(
    client: Any,
    skill_id: str,
    role_id: str = "",
    time_window: str = "",
    level: str = "",
    stack: str = "",
    limit: int = 50,
) -> dict:
    skill_rows = client.query(
        "MATCH (s:NormalizedSkill {concept_id:$skill_id}) RETURN properties(s) AS skill",
        {"skill_id": skill_id},
    )
    if not skill_rows:
        raise KeyError("技能不存在")
    role_rows = client.query(
        "MATCH (r:Role {role_id:$role_id}) RETURN properties(r) AS role",
        {"role_id": role_id},
    ) if role_id else []
    active_run_id = _active_normalization_run(client)
    evidence: list[dict] = []
    historical = False
    if role_id and time_window not in {"", "全量"}:
        historical_rows = client.query(
            """MATCH (:Role {role_id:$role_id})
                      -[:HAS_SKILL_SNAPSHOT {time_window:$time_window}]
                      ->(:NormalizedSkill {concept_id:$skill_id})
               RETURN count(*) AS n""",
            {"role_id": role_id, "time_window": time_window, "skill_id": skill_id},
        )
        historical = bool(historical_rows and int(historical_rows[0].get("n") or 0) > 0)
    if active_run_id and not level and not historical:
        evidence = client.query(
            """
            MATCH (skill:NormalizedSkill {concept_id:$skill_id})
            MATCH (ability:AbilityCandidate)
                  -[:NORMALIZES_TO {run_id:$run_id}]->(skill)
            MATCH (processed:ProcessedJD)-[edge:HAS_ABILITY]->(ability)
            MATCH (raw:RawJDVersion)-[:HAS_PROCESSING_RESULT]->(processed)
            OPTIONAL MATCH (role:Role {role_id:$role_id})
            WITH raw, edge, ability, role,
                 split(replace(coalesce(raw.publish_time_raw, ''), '-', '/'), '/') AS parts
            WITH raw, edge, ability, role,
                 CASE WHEN size(parts) >= 2
                      THEN parts[0] + 'Q' +
                           toString(toInteger((toInteger(parts[1]) - 1) / 3) + 1)
                      ELSE '' END AS jd_quarter
            WHERE ($role_id = '' OR raw.standard_role_id = $role_id)
              AND ($time_window = '' OR $time_window = '全量' OR jd_quarter = $time_window)
              AND ($stack = '' OR ability.tech_stack = $stack)
            WITH raw, edge, ability, jd_quarter
            LIMIT $candidate_limit
            RETURN $skill_id AS skill_id,
                   ability.name AS skill_name,
                   edge.raw_term AS raw_term,
                   edge.requirement_type AS requirement_type,
                   edge.evidence_quote AS evidence_quote,
                   edge.evidence_status AS evidence_status,
                   edge.confidence AS confidence,
                   edge.source AS source,
                   raw.version_id AS jd_id,
                   raw.title AS title,
                   raw.domain_role AS canonical_role,
                   raw.company_name AS company_name,
                   raw.publish_time_raw AS posted_at,
                   raw.source_category AS source_file,
                   raw.description AS description,
                   raw.tags AS tags,
                   CASE WHEN $time_window = '' OR $time_window = '全量'
                        THEN '全量' ELSE jd_quarter END AS time_window,
                   '' AS level_name,
                   ability.tech_stack AS tech_stack
            ORDER BY CASE edge.evidence_status
                WHEN 'VERIFIED' THEN 1
                WHEN 'LOW_CONFIDENCE' THEN 2
                ELSE 3 END,
                confidence DESC
            LIMIT $limit
            """,
            {
                "skill_id": skill_id,
                "run_id": active_run_id,
                "role_id": role_id,
                "time_window": time_window,
                "stack": stack,
                "candidate_limit": max(200, min(limit * 20, 2000)),
                "limit": max(1, min(limit, 200)),
            },
        )
    else:
        evidence = client.query(
            """
            MATCH (j:JD)-[e:MENTIONS_NORMALIZED_SKILL]->(:NormalizedSkill {concept_id:$skill_id})
            MATCH (j)-[:INSTANCE_OF]->(role:Role)
            MATCH (j)-[:SUPPORTS_PROFILE]->(profile:RoleProfile)
            MATCH (profile)-[:AT_LEVEL]->(levelNode:Level)
            MATCH (raw:Skill {skill_id:e.original_skill_id})
            WHERE ($role_id = '' OR role.role_id = $role_id)
              AND ($time_window = '' OR profile.time_window = $time_window)
              AND ($level = '' OR levelNode.name = $level)
              AND ($stack = '' OR raw.tech_stack = $stack)
            OPTIONAL MATCH (j)-[:POSTED_BY]->(company:Company)
            RETURN $skill_id AS skill_id, e.raw_term AS skill_name, e.raw_term AS raw_term,
                   e.requirement_type AS requirement_type, e.evidence_quote AS evidence_quote,
                   e.evidence_status AS evidence_status, e.confidence AS confidence, e.source AS source,
                   j.jd_id AS jd_id, j.title AS title, role.name AS canonical_role,
                   company.name AS company_name, j.posted_at AS posted_at, j.source_file AS source_file,
                   j.description AS description, j.tags AS tags, profile.time_window AS time_window,
                   levelNode.name AS level_name, raw.tech_stack AS tech_stack
            ORDER BY CASE e.evidence_status
                WHEN 'VERIFIED' THEN 1 WHEN 'LOW_CONFIDENCE' THEN 2 ELSE 3 END,
                confidence DESC, posted_at DESC
            LIMIT $limit
            """,
            {
                "skill_id": skill_id,
                "role_id": role_id,
                "time_window": time_window,
                "level": level,
                "stack": stack,
                "limit": max(1, min(limit, 200)),
            },
        )
    return {
        "skill": skill_rows[0]["skill"],
        "role": role_rows[0]["role"] if role_rows else {},
        "time_window": time_window,
        "level": level,
        "stack": stack,
        "evidence": evidence,
    }
