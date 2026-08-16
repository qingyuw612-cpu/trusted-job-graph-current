from __future__ import annotations

import base64
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .neo4j_filtered_view import load_facets, load_panorama, load_roles, load_skill_evidence


class Neo4jHttpClient:
    def __init__(self, uri: str, database: str, username: str, password: str, timeout: int = 30):
        self.endpoint = f"{uri.rstrip('/')}/db/{database}/query/v2"
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        self.headers = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self.timeout = timeout

    def query(
        self,
        statement: str,
        parameters: dict | None = None,
        access_mode: str = "Read",
    ) -> list[dict]:
        payload = json.dumps(
            {"statement": statement, "parameters": parameters or {}, "accessMode": access_mode},
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(self.endpoint, data=payload, headers=self.headers, method="POST")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise ValueError(f"Neo4j 查询失败：HTTP {error.code} {detail}") from error
        except URLError as error:
            raise ValueError(f"无法连接 Neo4j：{error.reason}") from error
        if result.get("errors"):
            raise ValueError(f"Neo4j 查询失败：{result['errors']}")
        data = result.get("data", {})
        fields = data.get("fields", [])
        return [dict(zip(fields, values)) for values in data.get("values", [])]


class Neo4jGraphRepository:
    def __init__(self, config_path: Path):
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.client = Neo4jHttpClient(
            config["http_uri"],
            config.get("database", "neo4j"),
            config.get("username", "neo4j"),
            config["password"],
            timeout=max(5, int(config.get("timeout_seconds", 120))),
        )

    @classmethod
    def from_connection(
        cls,
        http_uri: str,
        database: str,
        username: str,
        password: str,
        timeout: int = 120,
    ) -> "Neo4jGraphRepository":
        repository = cls.__new__(cls)
        repository.client = Neo4jHttpClient(
            http_uri,
            database,
            username,
            password,
            timeout=max(5, int(timeout)),
        )
        return repository

    def health(self) -> dict:
        rows = self.client.query(
            """
            CALL { MATCH (n:Role) RETURN count(n) AS roles }
            CALL { MATCH (n:RoleFamily) RETURN count(n) AS role_families }
            CALL { MATCH ()-[n:HAS_SKILL_SNAPSHOT]->(:NormalizedSkill) RETURN count(n) AS role_skill_snapshots }
            CALL { MATCH (n:NormalizedSkill) RETURN count(n) AS skills }
            CALL { MATCH (n:JD) RETURN count(n) AS jds }
            CALL { MATCH (n:ReviewTask) RETURN count(n) AS review_tasks }
            RETURN roles, role_families, role_skill_snapshots, skills, jds, review_tasks
            """
        )
        counts = rows[0] if rows else {}
        active_rows = self.client.query(
            """
            OPTIONAL MATCH (:NormalizationPointer {name:'core'})-[:ACTIVE]->(run:NormalizationRun)
            OPTIONAL MATCH (:Role)-[edge:HAS_CORE_SKILL {run_id:run.run_id}]->(:NormalizedSkill)
            RETURN run.run_id AS normalization_run_id,
                   run.status AS normalization_status,
                   count(edge) AS active_core_skills
            """
        )
        active = active_rows[0] if active_rows else {}
        counts["active_core_skills"] = int(active.get("active_core_skills") or 0)
        active_run_id = active.get("normalization_run_id") or ""
        if active_run_id:
            published_rows = self.client.query(
                """
                MATCH (role:Role)-[:HAS_CORE_SKILL {run_id:$run_id}]->(skill:NormalizedSkill)
                WITH collect(DISTINCT role) AS roles, collect(DISTINCT skill) AS skills
                RETURN size(roles) AS active_roles,
                       reduce(total=0, role IN roles |
                           total + toInteger(coalesce(role.document_count, 0))) AS active_jds,
                       size(skills) AS active_skills
                """,
                {"run_id": active_run_id},
            )
            published = published_rows[0] if published_rows else {}
            counts["legacy_jds"] = counts.get("jds", 0)
            counts["legacy_skills"] = counts.get("skills", 0)
            counts["jds"] = int(published.get("active_jds") or 0)
            counts["roles"] = int(published.get("active_roles") or 0)
            counts["skills"] = int(published.get("active_skills") or 0)
        return {
            "status": "ok",
            "state": "COMPLETED",
            "backend": "neo4j",
            "counts": counts,
            "summary": {
                "normalized_graph": True,
                "normalization_run_id": active.get("normalization_run_id") or "",
                "normalization_status": active.get("normalization_status") or "",
            },
        }

    def facets(self) -> dict:
        return load_facets(self.client)
        industries = self.client.query(
            "MATCH (n:Industry) RETURN n.industry_id AS industry_id, n.name AS industry_name ORDER BY n.name"
        )
        levels = self.client.query(
            "MATCH (n:Level) RETURN n.level_id AS level_id, n.name AS level_name ORDER BY n.name"
        )
        families = self.client.query(
            "MATCH (f:RoleFamily) RETURN f.family_id AS family_id, f.name AS family_name, "
            "f.domain_id AS domain_id, f.domain_name AS domain_name ORDER BY f.name"
        )
        windows = [
            row["time_window"]
            for row in self.client.query(
                "MATCH (:Role)-[s:HAS_SKILL_SNAPSHOT]->(:NormalizedSkill) "
                "RETURN s.time_window AS time_window, min(s.window_start) AS window_start "
                "ORDER BY window_start"
            )
        ]
        categories = [
            row["category"]
            for row in self.client.query(
                "MATCH (s:NormalizedSkill) WHERE s.category <> '' "
                "RETURN DISTINCT s.category AS category ORDER BY category"
            )
        ]
        return {
            "industries": industries,
            "levels": levels,
            "stacks": [],
            "categories": categories,
            "time_windows": windows,
            "families": families,
        }

    def roles(self, industry: str = "", level: str = "") -> list[dict]:
        del industry
        return load_roles(self.client, level)
        del industry, level
        return self.client.query(
            """
            MATCH (r:Role)
            OPTIONAL MATCH (r)-[:SUBTYPE_OF]->(parent:Role)
            OPTIONAL MATCH (family:RoleFamily)-[:HAS_ROLE]->(r)
            OPTIONAL MATCH (r)-[:HAS_PROFILE]->(profile:RoleProfile)
            RETURN r.role_id AS role_id, r.name AS role_name,
                   parent.role_id AS parent_role_id, parent.name AS parent_role_name,
                   family.family_id AS family_id, family.name AS family_name,
                   r.document_count AS document_count, r.company_count AS company_count,
                   count(DISTINCT profile) AS profile_count, max(profile.window_start) AS latest_window
            ORDER BY document_count DESC, role_name
            """
        )

    def panorama(
        self,
        industry: str = "",
        level: str = "",
        stack: str = "",
        category: str = "",
        time_window: str = "",
        role_id: str = "",
        min_support: float = 0.10,
        skill_limit: int = 30,
        role_limit: int = 14,
    ) -> dict:
        del industry, role_limit
        return load_panorama(
            self.client,
            level=level,
            stack=stack,
            category=category,
            time_window=time_window,
            role_id=role_id,
            min_support=min_support,
            skill_limit=skill_limit,
        )
        del industry, level, stack, category
        if not role_id:
            default = self.client.query(
                "MATCH (r:Role) RETURN r.role_id AS role_id ORDER BY CASE r.name WHEN '产品经理' THEN 0 ELSE 1 END, r.document_count DESC LIMIT 1"
            )
            role_id = default[0]["role_id"] if default else ""
        role_rows = self.client.query(
            """
            MATCH path=(r:Role)-[:SUBTYPE_OF*0..5]->(root:Role {role_id: $role_id})
            WITH DISTINCT r
            OPTIONAL MATCH (r)-[:SUBTYPE_OF]->(parent:Role)
            OPTIONAL MATCH (family:RoleFamily)-[:HAS_ROLE]->(r)
            RETURN r.role_id AS role_id, r.name AS role_name,
                   parent.role_id AS parent_role_id, parent.name AS parent_role_name,
                   family.family_id AS family_id, family.name AS family_name,
                   family.domain_id AS domain_id, family.domain_name AS domain_name,
                   r.document_count AS document_count, r.company_count AS company_count
            ORDER BY CASE WHEN r.role_id = $role_id THEN 0 ELSE 1 END, r.name LIMIT $role_limit
            """,
            {"role_id": role_id, "role_limit": max(1, min(role_limit, 80))},
        )
        if not role_rows:
            raise KeyError("岗位不存在")
        role_ids = [row["role_id"] for row in role_rows]
        snapshots = self.client.query(
            """
            MATCH (r:Role)-[snapshot:HAS_SKILL_SNAPSHOT]->(skill:NormalizedSkill)
            WHERE r.role_id IN $role_ids
            RETURN r.role_id AS role_id, skill.concept_id AS skill_id,
                   skill.canonical_name AS canonical_name,
                   skill.category AS competency_category, '' AS tech_stack,
                   snapshot.time_window AS time_window, snapshot.window_start AS window_start,
                   snapshot.final_score AS adjusted_support, snapshot.delta AS delta,
                   snapshot.trend AS trend, 'core' AS tier,
                   'HAS_CORE_SKILL' AS relation,
                   snapshot.verified_jd_count AS evidence_count,
                   snapshot.rank AS skill_rank
            """,
            {"role_ids": role_ids},
        )
        window_order = {}
        for row in snapshots:
            if row.get("time_window"):
                window_order[row["time_window"]] = min(
                    window_order.get(row["time_window"], row.get("window_start") or ""),
                    row.get("window_start") or "",
                )
        available_windows = [item[0] for item in sorted(window_order.items(), key=lambda item: item[1])]
        latest_by_role = {}
        for row in snapshots:
            latest_by_role[row["role_id"]] = max(latest_by_role.get(row["role_id"], ""), row.get("window_start") or "")
        filtered = [
            row
            for row in snapshots
            if float(row.get("adjusted_support") or 0) >= min_support
            and (
                row.get("time_window") == time_window
                if time_window
                else row.get("window_start") == latest_by_role.get(row["role_id"])
            )
        ]
        selected = []
        for selected_role_id in role_ids:
            role_skills = [row for row in filtered if row["role_id"] == selected_role_id]
            role_skills.sort(
                key=lambda row: (
                    int(row.get("skill_rank") or 9999),
                    -float(row.get("adjusted_support") or 0),
                )
            )
            selected.extend(role_skills[: max(1, min(skill_limit, 50))])

        nodes: list[dict] = []
        edges: list[dict] = []
        family_ids = set()
        for row in role_rows:
            family_id = row.get("family_id") or ""
            if family_id and family_id not in family_ids:
                domain_node_id = f"domain:{row.get('domain_id') or 'it'}"
                family_node_id = f"family:{family_id}"
                if not any(node["id"] == domain_node_id for node in nodes):
                    nodes.append({"id": domain_node_id, "entity_id": row.get("domain_id") or "it", "type": "domain", "label": row.get("domain_name") or "新一代信息技术"})
                nodes.append({"id": family_node_id, "entity_id": family_id, "type": "family", "label": row.get("family_name") or "岗位族"})
                edges.append({"id": f"domain-family:{family_id}", "source": domain_node_id, "target": family_node_id, "relation": "HAS_FAMILY"})
                family_ids.add(family_id)
        visible_role_ids = set(role_ids)
        for row in role_rows:
            nodes.append({
                "id": row["role_id"], "entity_id": row["role_id"], "type": "role", "label": row["role_name"],
                "parent_role_id": row.get("parent_role_id") or "", "family_id": row.get("family_id") or "",
                "jd_count": row.get("document_count") or 0, "company_count": row.get("company_count") or 0,
                "focused": row["role_id"] == role_id,
            })
            if row.get("parent_role_id") in visible_role_ids:
                source, relation = row["parent_role_id"], "HAS_SUBTYPE"
            else:
                source, relation = f"family:{row.get('family_id') or ''}", "HAS_ROLE"
            edges.append({"id": f"role-tree:{source}:{row['role_id']}", "source": source, "target": row["role_id"], "relation": relation})
        categories = set()
        for row in selected:
            category_name = row.get("competency_category") or "其他能力"
            category_id = f"category:{row['role_id']}:{category_name}"
            if category_id not in categories:
                nodes.append({"id": category_id, "entity_id": category_name, "role_id": row["role_id"], "type": "category", "label": category_name})
                edges.append({"id": f"role-category:{row['role_id']}:{category_name}", "source": row["role_id"], "target": category_id, "relation": "HAS_SKILL_GROUP"})
                categories.add(category_id)
            visual_skill_id = f"skill:{row['role_id']}:{row['skill_id']}"
            nodes.append({
                "id": visual_skill_id, "entity_id": row["skill_id"], "role_id": row["role_id"], "type": "skill",
                "label": row["canonical_name"], "stack": row.get("tech_stack") or "", "category": category_name,
                "support": row.get("adjusted_support") or 0, "tier": row.get("tier") or "emerging",
                "trend": row.get("trend") or "STABLE", "delta": row.get("delta") or 0,
                "evidence_count": row.get("evidence_count") or 0, "time_window": row.get("time_window") or "",
            })
            edges.append({
                "id": f"category-skill:{row['role_id']}:{row['skill_id']}", "source": category_id,
                "target": visual_skill_id, "relation": row.get("relation") or "REQUIRES_SKILL",
                "tier": row.get("tier") or "emerging", "support": row.get("adjusted_support") or 0,
            })
        return {
            "nodes": nodes, "edges": edges, "related_edges": [],
            "stats": {"roles": len(role_rows), "skills": len(selected), "categories": len(categories), "edges": len(edges), "evidence": sum(int(row.get("evidence_count") or 0) for row in selected)},
            "quality": {"backend": "neo4j", "normalized_graph": True}, "available_windows": available_windows,
            "filters": {"time_window": time_window, "role_id": role_id, "min_support": min_support, "skill_limit": skill_limit},
        }

    def role_timeline(self, role_id: str) -> dict:
        role_rows = self.client.query("MATCH (r:Role {role_id:$role_id}) RETURN properties(r) AS role", {"role_id": role_id})
        if not role_rows:
            raise KeyError("岗位不存在")
        rows = self.client.query(
            """
            MATCH (:Role {role_id:$role_id})-[snapshot:HAS_SKILL_SNAPSHOT]->(skill:NormalizedSkill)
            RETURN snapshot.time_window AS time_window, snapshot.window_start AS window_start,
                   skill.concept_id AS skill_id, skill.canonical_name AS canonical_name,
                   snapshot.final_score AS adjusted_support, 0 AS previous_support,
                   snapshot.delta AS delta, snapshot.trend AS trend,
                   snapshot.verified_jd_count AS evidence_count, 'core' AS tier,
                   snapshot.rank AS skill_rank
            ORDER BY window_start, skill_rank
            """,
            {"role_id": role_id},
        )
        windows = {}
        for row in rows:
            window = windows.setdefault(row["time_window"], {"time_window": row["time_window"], "window_start": row["window_start"], "skills": []})
            if len(window["skills"]) < 50:
                window["skills"].append(row)
        return {"role": role_rows[0]["role"], "windows": list(windows.values())}

    def skill_evidence(
        self,
        skill_id: str,
        role_id: str = "",
        time_window: str = "",
        level: str = "",
        stack: str = "",
        limit: int = 50,
    ) -> dict:
        return load_skill_evidence(
            self.client,
            skill_id,
            role_id=role_id,
            time_window=time_window,
            level=level,
            stack=stack,
            limit=limit,
        )
        skill_rows = self.client.query(
            "MATCH (s:NormalizedSkill {concept_id:$skill_id}) RETURN properties(s) AS skill",
            {"skill_id": skill_id},
        )
        if not skill_rows:
            raise KeyError("技能不存在")
        role_rows = self.client.query("MATCH (r:Role {role_id:$role_id}) RETURN properties(r) AS role", {"role_id": role_id}) if role_id else []
        evidence = self.client.query(
            """
            MATCH (j:JD)-[e:MENTIONS_NORMALIZED_SKILL]->(:NormalizedSkill {concept_id:$skill_id})
            MATCH (j)-[:INSTANCE_OF]->(role:Role)
            OPTIONAL MATCH (j)-[:POSTED_BY]->(company:Company)
            OPTIONAL MATCH (j)-[:SUPPORTS_PROFILE]->(profile:RoleProfile)
            WITH j, e, role, company, profile
            WHERE ($role_id = '' OR role.role_id = $role_id)
              AND ($time_window = '' OR profile.time_window = $time_window)
            RETURN $skill_id AS skill_id, e.raw_term AS skill_name, e.raw_term AS raw_term,
                   e.requirement_type AS requirement_type, e.evidence_quote AS evidence_quote,
                   e.evidence_status AS evidence_status, e.confidence AS confidence, e.source AS source,
                   j.jd_id AS jd_id, j.title AS title, role.name AS canonical_role,
                   company.name AS company_name, j.posted_at AS posted_at, j.source_file AS source_file,
                   j.description AS description, j.tags AS tags, profile.time_window AS time_window
            ORDER BY confidence DESC, posted_at DESC LIMIT $limit
            """,
            {"skill_id": skill_id, "role_id": role_id, "time_window": time_window, "limit": max(1, min(limit, 200))},
        )
        return {"skill": skill_rows[0]["skill"], "role": role_rows[0]["role"] if role_rows else {}, "time_window": time_window, "evidence": evidence}

    def latest_profiles(self, role_id: str) -> dict:
        role_rows = self.client.query(
            "MATCH (r:Role {role_id:$role_id}) RETURN properties(r) AS role",
            {"role_id": role_id},
        )
        if not role_rows:
            raise KeyError("岗位不存在")
        profile_rows = self.client.query(
            """
            MATCH (r:Role {role_id:$role_id})-[:HAS_PROFILE]->(profile:RoleProfile)
            WITH r, max(profile.window_start) AS latest_window
            MATCH (r)-[:HAS_PROFILE]->(profile:RoleProfile)
            WHERE profile.window_start = latest_window
            RETURN properties(profile) AS profile
            ORDER BY profile.industry_name, profile.level_name
            """,
            {"role_id": role_id},
        )
        profiles = []
        for row in profile_rows:
            profile = row["profile"]
            profile["skills"] = []
            profiles.append(profile)
        summary_skills = self.client.query(
            """
            MATCH (:NormalizationPointer {name:'core'})-[:ACTIVE]->(run:NormalizationRun)
            MATCH (:Role {role_id:$role_id})
                  -[edge:HAS_CORE_SKILL {run_id:run.run_id}]->(skill:NormalizedSkill)
            RETURN skill.concept_id AS skill_id, skill.canonical_name AS canonical_name,
                   skill.category AS competency_category, '' AS tech_stack,
                   edge.final_score AS adjusted_support, edge.verified_jd_count AS evidence_count,
                   edge.rank AS skill_rank
            ORDER BY skill_rank LIMIT 50
            """,
            {"role_id": role_id},
        )
        raw_titles = self.client.query(
            """
            MATCH (jd:JD)-[:INSTANCE_OF]->(:Role {role_id:$role_id})
            WHERE coalesce(jd.duplicate_of, '') = ''
            RETURN jd.title AS title, count(*) AS amount
            ORDER BY amount DESC, title LIMIT 8
            """,
            {"role_id": role_id},
        )
        return {"role": role_rows[0]["role"], "profiles": profiles, "summary_skills": summary_skills, "raw_titles": raw_titles}

    def evolution(self, role_id: str) -> list[dict]:
        return self.client.query(
            """
            MATCH (:Role {role_id:$role_id})-[snapshot:HAS_SKILL_SNAPSHOT]->(skill:NormalizedSkill)
            WHERE snapshot.trend IN ['emerging', 'rising', 'falling']
            RETURN skill.concept_id AS skill_id, skill.canonical_name AS canonical_name,
                   snapshot.time_window AS current_window,
                   snapshot.trend AS change_type, snapshot.delta AS delta,
                   snapshot.final_score AS current_support,
                   snapshot.final_score - snapshot.delta AS previous_support
            ORDER BY current_window, abs(delta) DESC
            """,
            {"role_id": role_id},
        )

    def review_tasks(self, status: str = "PENDING", limit: int = 100) -> list[dict]:
        rows = self.client.query(
            """
            MATCH (task:ReviewTask)
            WHERE $status = '' OR task.status = $status
            RETURN properties(task) AS task
            ORDER BY task.created_at DESC LIMIT $limit
            """,
            {"status": status, "limit": max(1, min(limit, 500))},
        )
        return [row["task"] for row in rows]

    def decide_review(self, task_id: str, status: str, decision: str) -> dict:
        del task_id, status, decision
        raise ValueError("Neo4j正式查询版暂不在网页中修改审核任务")
