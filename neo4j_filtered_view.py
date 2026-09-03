from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from raw_jd_layer.archive import hydrate_raw_rows


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
    # NormalizedSkill is the canonical production entity.  The old Skill
    # nodes may still exist in historical imports, but must not drive the
    # facets for a published normalized graph.
    if active_run_id:
        stack_rows = client.query(
            "MATCH (:Role)-[:HAS_CORE_SKILL {run_id:$run_id}]->(s:NormalizedSkill) "
            "WHERE coalesce(s.tech_stack, '') <> '' "
            "RETURN DISTINCT s.tech_stack AS tech_stack ORDER BY tech_stack",
            {"run_id": active_run_id},
        )
    else:
        stack_rows = client.query(
            "MATCH (s:NormalizedSkill) WHERE coalesce(s.tech_stack, '') <> '' "
            "RETURN DISTINCT s.tech_stack AS tech_stack ORDER BY tech_stack"
        )
    stacks = [row["tech_stack"] for row in stack_rows if row.get("tech_stack")]
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
    stack: str = "",
) -> dict | None:
    role_id = role["role_id"]
    skills = client.query(
        """
        MATCH (:Role {role_id:$role_id})
              -[edge:HAS_CORE_SKILL {run_id:$run_id}]->(skill:NormalizedSkill)
        WHERE edge.final_score >= $min_support
          AND ($category = '' OR skill.category = $category)
          AND ($stack = '' OR skill.tech_stack = $stack)
        RETURN skill.concept_id AS skill_id,
               skill.canonical_name AS canonical_name,
               skill.category AS competency_category,
               skill.tech_stack AS tech_stack,
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
            "stack": stack,
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
                "stack": row.get("tech_stack") or "",
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
            "stack": stack,
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
    stack: str,
    min_support: float,
    skill_limit: int,
    available_windows: list[str],
) -> dict:
    parameters = {
        "role_id": role["role_id"], "time_window": time_window, "stack": stack,
        "category": category, "min_support": max(0.0, min(float(min_support), 1.0)),
        "skill_limit": max(1, min(int(skill_limit), 50)),
    }
    rows = client.query(
        """
        MATCH (:Role {role_id:$role_id})-[e:HAS_SKILL_SNAPSHOT {time_window:$time_window}]
              ->(s:NormalizedSkill)
        WHERE e.final_score >= $min_support AND ($category='' OR s.category=$category)
          AND ($stack='' OR s.tech_stack=$stack)
        RETURN s.concept_id AS skill_id,s.canonical_name AS canonical_name,
               s.category AS competency_category,s.tech_stack AS tech_stack,
               e.final_score AS adjusted_support,
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
                      "label":row["canonical_name"],"stack":row.get("tech_stack") or "",
                      "category":category_name,"support":row.get("adjusted_support") or 0,
                      "tier":"historical_snapshot","trend":row.get("trend") or "STABLE","delta":row.get("delta") or 0,
                      "evidence_count":row.get("evidence_count") or 0,"time_window":time_window})
        edges.append({"id":f"category-skill:{role['role_id']}:{row['skill_id']}","source":category_id,"target":skill_id,
                      "relation":"HAS_CORE_SKILL","support":row.get("adjusted_support") or 0})
    return {"nodes":nodes,"edges":edges,"related_edges":[],
            "stats":{"roles":1,"skills":len(rows),"categories":len(category_ids),"edges":len(edges),
                     "evidence":sum(int(x.get("evidence_count") or 0) for x in rows),"filtered_jds":total_jds},
            "quality":{"backend":"neo4j","normalized_graph":True,"historical_snapshot":True},
            "available_windows":available_windows,
            "filters":{"role_id":role["role_id"],"category":category,"stack":stack,"time_window":time_window,
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
                  MATCH (processed)-[:HAS_ABILITY]->(:AbilityCandidate)
                        -[:NORMALIZES_TO {run_id:$run_id}]->(stackSkill:NormalizedSkill)
                  WHERE stackSkill.tech_stack = $stack
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
          AND ($stack = '' OR skill.tech_stack = $stack)
          AND ($category = '' OR skill.category = $category)
        WITH skill,
             count(DISTINCT raw.version_id) AS jd_count,
             count(DISTINCT raw.company_id) AS company_count,
             count(mention) AS evidence_count,
        WITH skill, jd_count, company_count, evidence_count,
             toFloat(jd_count) / $total_jds AS adjusted_support
        WHERE adjusted_support >= $min_support
        RETURN skill.concept_id AS skill_id,
               skill.canonical_name AS canonical_name,
               skill.category AS competency_category,
               skill.tech_stack AS tech_stack,
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
        if not requested_window:
            published = _published_panorama(
                client,
                role_rows[0],
                active_run_id,
                category,
                min_support,
                skill_limit,
                available_windows,
                stack=stack,
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
                    client, role_rows[0], requested_window, category, stack,
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
              MATCH (j)-[:MENTIONS_NORMALIZED_SKILL]->(stackSkill:NormalizedSkill)
              WHERE stackSkill.tech_stack = $stack
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
        WHERE ($level = '' OR l.name = $level)
          AND ($time_window = '' OR p.time_window = $time_window)
          AND coalesce(j.duplicate_of, '') = ''
          AND ($stack = '' OR skill.tech_stack = $stack)
          AND ($category = '' OR skill.category = $category)
        WITH r, skill, count(DISTINCT j) AS jd_count,
             count(DISTINCT e) AS evidence_count,
             count(DISTINCT j.company_name) AS company_count,
        WITH r, skill, jd_count, evidence_count, company_count,
             toFloat(jd_count) / $total_jds AS adjusted_support
        WHERE adjusted_support >= $min_support
        OPTIONAL MATCH (r)-[snapshot:HAS_SKILL_SNAPSHOT {time_window:$time_window}]->(skill)
        RETURN r.role_id AS role_id, skill.concept_id AS skill_id,
               skill.canonical_name AS canonical_name,
               skill.category AS competency_category,
               skill.tech_stack AS tech_stack,
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
            WITH raw, edge, ability, role, skill,
                 split(replace(coalesce(raw.publish_time_raw, ''), '-', '/'), '/') AS parts
            WITH raw, edge, ability, role, skill,
                 CASE WHEN size(parts) >= 2
                      THEN parts[0] + 'Q' +
                           toString(toInteger((toInteger(parts[1]) - 1) / 3) + 1)
                      ELSE '' END AS jd_quarter
            WHERE ($role_id = '' OR raw.standard_role_id = $role_id)
              AND ($time_window = '' OR $time_window = '全量' OR jd_quarter = $time_window)
              AND ($stack = '' OR skill.tech_stack = $stack)
            WITH raw, edge, ability, skill, jd_quarter
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
                   skill.tech_stack AS tech_stack
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
            MATCH (j:JD)-[e:MENTIONS_NORMALIZED_SKILL]->(skill:NormalizedSkill {concept_id:$skill_id})
            MATCH (j)-[:INSTANCE_OF]->(role:Role)
            MATCH (j)-[:SUPPORTS_PROFILE]->(profile:RoleProfile)
            MATCH (profile)-[:AT_LEVEL]->(levelNode:Level)
            WHERE ($role_id = '' OR role.role_id = $role_id)
              AND ($time_window = '' OR profile.time_window = $time_window)
              AND ($level = '' OR levelNode.name = $level)
              AND ($stack = '' OR skill.tech_stack = $stack)
            OPTIONAL MATCH (j)-[:POSTED_BY]->(company:Company)
            RETURN $skill_id AS skill_id, e.raw_term AS skill_name, e.raw_term AS raw_term,
                   e.requirement_type AS requirement_type, e.evidence_quote AS evidence_quote,
                   e.evidence_status AS evidence_status, e.confidence AS confidence, e.source AS source,
                   j.jd_id AS jd_id, j.title AS title, role.name AS canonical_role,
                   company.name AS company_name, j.posted_at AS posted_at, j.source_file AS source_file,
                   j.description AS description, j.tags AS tags, profile.time_window AS time_window,
                   levelNode.name AS level_name, skill.tech_stack AS tech_stack
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


def _safe_51job_url(value: Any) -> str:
    """Return a browser-safe 51job vacancy URL, or an empty string."""
    raw_url = str(value or "").strip()
    if not raw_url:
        return ""
    try:
        parsed = urlsplit(raw_url)
    except ValueError:
        return ""
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() not in {"http", "https"}:
        return ""
    # Do not reflect credentials or an unexpected port into an outbound link.
    # The hostname check alone is insufficient for URLs such as
    # https://user:pass@jobs.51job.com/..., which browsers still accept.
    if parsed.username or parsed.password:
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    if port not in {None, 80, 443}:
        return ""
    if hostname != "51job.com" and not hostname.endswith(".51job.com"):
        return ""
    # Rebuild netloc from the validated hostname/port so odd casing and
    # user-info cannot leak into the returned contract.
    netloc = hostname
    if port is not None and not ((parsed.scheme.lower() == "http" and port == 80) or (parsed.scheme.lower() == "https" and port == 443)):
        netloc = f"{netloc}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))


def load_role_openings(
    client: Any,
    role_id: str,
    page: int = 1,
    page_size: int = 12,
    limit: int | None = None,
) -> dict:
    """Load a deterministic page of recent 51job vacancies for a role.

    ``limit`` remains accepted for older callers, but new callers should use
    ``page``/``page_size``.  The archive is hydrated only after the page query
    has selected its rows, so a large vacancy history does not turn each API
    request into a full archive read.
    """
    if limit is not None:
        page_size = limit
    bounded_page = max(1, int(page))
    bounded_page_size = max(1, min(int(page_size), 50))
    skip = (bounded_page - 1) * bounded_page_size
    role_rows = client.query(
        "MATCH (role:Role {role_id:$role_id}) RETURN properties(role) AS role",
        {"role_id": role_id},
    )
    if not role_rows:
        raise KeyError("岗位不存在")

    candidates = client.query(
        """
        MATCH (role:Role {role_id:$role_id})
        MATCH (job:RawJob)-[:CURRENT_VERSION]->(raw:RawJDVersion)
        MATCH (raw)-[:HAS_PROCESSING_RESULT]->(:ProcessedJD {status:'COMPLETED'})
        WHERE (
              raw.standard_role_id = $role_id
              OR (
                 coalesce(raw.standard_role_id, '') = ''
                 AND (
                    (coalesce(raw.domain_role, '') <> ''
                     AND (raw.domain_role IN [role.name, role.role_name]
                          OR raw.domain_role IN coalesce(role.source_role_names, [])))
                    OR
                    (coalesce(raw.source_normalized_role, '') <> ''
                     AND (raw.source_normalized_role IN [role.name, role.role_name]
                          OR raw.source_normalized_role IN coalesce(role.source_role_names, [])))
                    OR
                    (coalesce(raw.declared_role, '') <> ''
                     AND (raw.declared_role IN [role.name, role.role_name]
                          OR raw.declared_role IN coalesce(role.source_role_names, [])))
                 )
              )
        )
          AND toLower(coalesce(raw.source_platform, job.source_platform, '')) IN
              ['51job', '前程无忧', '51job.com']
          AND coalesce(raw.job_link, '') <> ''
          AND raw.job_link =~ '(?i)^https?://([a-z0-9-]+\\.)*51job\\.com(/|\\?|#|$).*'
        OPTIONAL MATCH (job)-[:POSTED_BY]->(company:Company)
        WITH CASE
               WHEN coalesce(raw.source_job_id, job.source_job_id, '') <> ''
               THEN coalesce(raw.source_job_id, job.source_job_id)
               ELSE raw.job_link
             END AS dedup_key,
             raw, job, company
        ORDER BY raw.publish_time_raw DESC, raw.observed_epoch DESC, raw.version_id
        WITH dedup_key, collect({
               source_job_id: coalesce(raw.source_job_id, job.source_job_id, ''),
               version_id: raw.version_id,
               company_id: coalesce(raw.company_id, job.company_id, company.company_id, ''),
               source_platform: coalesce(raw.source_platform, job.source_platform, '51job'),
               title: raw.title,
               company_name: coalesce(raw.company_name, job.company_name, company.name, ''),
               location: raw.location,
               salary: raw.salary,
               education: raw.education,
               experience: raw.experience,
               posted_at: raw.publish_time_raw,
               collected_at: raw.collected_at_raw,
               description: raw.description,
               raw_archive_uri: raw.raw_archive_uri,
               job_url: raw.job_link,
               observed_epoch: raw.observed_epoch
             })[0] AS row
        WITH row
        ORDER BY row.posted_at DESC, row.observed_epoch DESC, row.version_id
        SKIP $skip LIMIT $page_size
        RETURN row.source_job_id AS source_job_id,
               row.version_id AS version_id,
               row.company_id AS company_id,
               row.source_platform AS source_platform,
               row.title AS title,
               row.company_name AS company_name,
               row.location AS location,
               row.salary AS salary,
               row.education AS education,
               row.experience AS experience,
               row.posted_at AS posted_at,
               row.collected_at AS collected_at,
               row.description AS description,
               row.raw_archive_uri AS raw_archive_uri,
               row.job_url AS job_url
        """,
        {"role_id": role_id, "skip": skip, "page_size": bounded_page_size},
    )

    # Count with the same graph predicates, before archive hydration.  The
    # fallback identity mirrors the public deduplication rule: source job id
    # wins, otherwise the canonical 51job URL identifies the opening.
    count_rows = client.query(
        """
        MATCH (role:Role {role_id:$role_id})
        MATCH (job:RawJob)-[:CURRENT_VERSION]->(raw:RawJDVersion)
        MATCH (raw)-[:HAS_PROCESSING_RESULT]->(:ProcessedJD {status:'COMPLETED'})
        WHERE (
              raw.standard_role_id = $role_id
              OR (
                 coalesce(raw.standard_role_id, '') = ''
                 AND (
                    (coalesce(raw.domain_role, '') <> ''
                     AND (raw.domain_role IN [role.name, role.role_name]
                          OR raw.domain_role IN coalesce(role.source_role_names, [])))
                    OR
                    (coalesce(raw.source_normalized_role, '') <> ''
                     AND (raw.source_normalized_role IN [role.name, role.role_name]
                          OR raw.source_normalized_role IN coalesce(role.source_role_names, [])))
                    OR
                    (coalesce(raw.declared_role, '') <> ''
                     AND (raw.declared_role IN [role.name, role.role_name]
                          OR raw.declared_role IN coalesce(role.source_role_names, [])))
                 )
              )
        )
          AND toLower(coalesce(raw.source_platform, job.source_platform, '')) IN
              ['51job', '前程无忧', '51job.com']
          AND coalesce(raw.job_link, '') <> ''
          AND raw.job_link =~ '(?i)^https?://([a-z0-9-]+\\.)*51job\\.com(/|\\?|#|$).*'
        RETURN count(DISTINCT CASE
          WHEN coalesce(raw.source_job_id, job.source_job_id, '') <> ''
          THEN coalesce(raw.source_job_id, job.source_job_id)
          ELSE raw.job_link
        END) AS total
        """,
        {"role_id": role_id},
    )
    try:
        candidates = hydrate_raw_rows(candidates)
    except (OSError, ValueError):
        # A missing historical archive must not take down the public graph.
        # The card still exposes structured fields and the authoritative link.
        candidates = [dict(row) for row in candidates]

    openings = []
    seen: set[tuple[str, str]] = set()
    for row in candidates:
        job_url = _safe_51job_url(row.get("job_url"))
        if not job_url:
            continue
        source_job_id = str(row.get("source_job_id") or "").strip()
        # The source vacancy id is the strongest identity.  For legacy rows
        # without it, the canonicalized URL is stable and prevents repeated
        # versions of one opening from occupying several cards.
        key = ("source_job_id", source_job_id) if source_job_id else ("job_url", job_url)
        if key in seen:
            continue
        seen.add(key)
        # Hydration may restore the complete archived source row.  Keep the
        # public contract deliberately narrow: internal crawl/search metadata
        # (notably crawl_keyword and archive paths) must never leak through a
        # public openings response.
        item = {
            field: row.get(field)
            for field in (
                "source_job_id",
                "version_id",
                "company_id",
                "source_platform",
                "title",
                "company_name",
                "location",
                "salary",
                "education",
                "experience",
                "posted_at",
                "collected_at",
                "description",
            )
        }
        item["source_job_id"] = source_job_id
        item["job_url"] = job_url
        item["source_platform"] = "前程无忧"
        item["availability"] = "CHECK_ON_SOURCE"
        item["availability_label"] = "是否在招以平台页面为准"
        openings.append(item)
    # A defensive dedup pass is retained for old Neo4j indexes and lightweight
    # test clients that do not enforce the query's deterministic page window.
    # In production the query is already bounded before hydration.
    openings = openings[:bounded_page_size]
    try:
        total = max(0, int((count_rows[0] or {}).get("total", 0))) if count_rows else 0
    except (TypeError, ValueError):
        total = 0
    if not total:
        total = len(seen)
    total_pages = (total + bounded_page_size - 1) // bounded_page_size

    return {
        "role": role_rows[0]["role"],
        "source_platform": "前程无忧",
        "openings": openings,
        "items": openings,
        "returned": len(openings),
        "page": bounded_page,
        "page_size": bounded_page_size,
        "total": total,
        "total_pages": total_pages,
        "has_previous": bounded_page > 1 and total_pages > 0,
        "has_next": bounded_page < total_pages,
        "disclaimer": "招聘状态可能变化，是否仍在招聘以前程无忧原页面为准。",
    }
