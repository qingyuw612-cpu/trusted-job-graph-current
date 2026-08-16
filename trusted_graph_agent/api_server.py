from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


class GraphRepository:
    def __init__(self, database_path: Path):
        self.database_path = database_path

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    def health(self) -> dict:
        with self._connect() as connection:
            run = connection.execute("SELECT run_id, state, payload_json FROM runs LIMIT 1").fetchone()
            counts = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("role_families", "roles", "role_skill_snapshots", "skills", "jds", "review_tasks")
            }
        payload = json.loads(run["payload_json"]) if run else {}
        return {"status": "ok", "run_id": run["run_id"] if run else "", "state": run["state"] if run else "", "counts": counts, "summary": payload.get("summary", {})}

    def facets(self) -> dict:
        with self._connect() as connection:
            industries = [dict(row) for row in connection.execute("SELECT * FROM industries ORDER BY industry_name")]
            levels = [dict(row) for row in connection.execute("SELECT * FROM levels ORDER BY CASE level_name WHEN '实习/应届' THEN 1 WHEN '初级' THEN 2 WHEN '中级' THEN 3 WHEN '高级' THEN 4 WHEN '专家' THEN 5 WHEN '管理岗' THEN 6 ELSE 7 END")]
            stacks = [row[0] for row in connection.execute("SELECT DISTINCT tech_stack FROM skills WHERE tech_stack <> '' ORDER BY tech_stack")]
            categories = [row[0] for row in connection.execute("SELECT DISTINCT competency_category FROM skills WHERE competency_category <> '' ORDER BY competency_category")]
            windows = [row[0] for row in connection.execute("SELECT DISTINCT time_window FROM role_profiles ORDER BY window_start")]
            families = [dict(row) for row in connection.execute("SELECT * FROM role_families ORDER BY family_name")]
        return {"industries": industries, "levels": levels, "stacks": stacks, "categories": categories, "time_windows": windows, "families": families}

    def roles(self, industry: str = "", level: str = "") -> list[dict]:
        conditions = []
        values: list[str] = []
        if industry:
            conditions.append("(p.industry_id = ? OR p.industry_name = ?)")
            values.extend([industry, industry])
        if level:
            conditions.append("(p.level_id = ? OR p.level_name = ?)")
            values.extend([level, level])
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        sql = f"""
            SELECT r.role_id, r.role_name, r.parent_role_id, r.parent_role_name,
                   r.family_id, r.family_name, r.domain_id, r.domain_name,
                   r.document_count, r.company_count, r.industries,
                   COUNT(DISTINCT p.profile_id) AS profile_count, MAX(p.window_start) AS latest_window
            FROM roles r JOIN role_profiles p ON p.role_id = r.role_id
            {where}
            GROUP BY r.role_id ORDER BY r.document_count DESC, r.role_name
        """
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(sql, values)]

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
        del industry, level, stack, category
        with self._connect() as connection:
            if not role_id:
                default_role = connection.execute(
                    "SELECT role_id FROM roles ORDER BY CASE role_name WHEN '产品经理' THEN 0 ELSE 1 END, document_count DESC LIMIT 1"
                ).fetchone()
                role_id = default_role["role_id"] if default_role else ""
            role_rows = [
                dict(row)
                for row in connection.execute(
                    """
                    WITH RECURSIVE role_tree(role_id) AS (
                        SELECT ? UNION ALL
                        SELECT rr.child_role_id FROM role_relations rr JOIN role_tree t ON rr.parent_role_id = t.role_id
                    )
                    SELECT r.* FROM roles r JOIN role_tree t ON t.role_id = r.role_id
                    ORDER BY CASE WHEN r.role_id = ? THEN 0 ELSE 1 END, r.role_name LIMIT ?
                    """,
                    (role_id, role_id, max(1, min(role_limit, 80))),
                )
            ]
            if not role_rows:
                raise KeyError("岗位不存在")
            role_ids = [row["role_id"] for row in role_rows]
            placeholders = ",".join("?" for _ in role_ids)
            available_windows = [
                row[0]
                for row in connection.execute(
                    f"SELECT time_window FROM role_skill_snapshots WHERE role_id IN ({placeholders}) GROUP BY time_window ORDER BY MIN(window_start)",
                    role_ids,
                )
            ]
            normalized_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'normalized_role_skills'"
            ).fetchone()
            if normalized_table:
                values = [*role_ids, min_support, max(1, min(skill_limit, 50))]
                snapshot_table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'normalized_role_skill_snapshots'"
                ).fetchone()
                if time_window and snapshot_table:
                    values = [*role_ids, time_window, min_support, max(1, min(skill_limit, 50))]
                    skill_rows = [
                        dict(row)
                        for row in connection.execute(
                            f"""
                            SELECT nss.role_id, nss.concept_id AS skill_id, ns.canonical_name,
                                   ns.category AS competency_category, '' AS tech_stack,
                                   nss.final_score AS adjusted_support, 'HAS_CORE_SKILL' AS relation,
                                   'dynamic' AS tier, nss.trend, nss.delta,
                                   nss.verified_jd_count AS evidence_count, nss.time_window,
                                   nss.rank AS skill_rank
                            FROM normalized_role_skill_snapshots nss
                            JOIN normalized_skills ns ON ns.concept_id = nss.concept_id
                            WHERE nss.role_id IN ({placeholders}) AND nss.time_window = ?
                              AND nss.final_score >= ? AND nss.rank <= ?
                            ORDER BY nss.role_id, nss.rank
                            """,
                            values,
                        )
                    ]
                else:
                    skill_rows = [
                        dict(row)
                        for row in connection.execute(
                            f"""
                            SELECT nrs.role_id, nrs.concept_id AS skill_id, ns.canonical_name,
                                   ns.category AS competency_category, '' AS tech_stack,
                                   nrs.final_score AS adjusted_support, 'HAS_CORE_SKILL' AS relation,
                                   'core' AS tier, 'stable' AS trend, 0 AS delta,
                                   nrs.verified_jd_count AS evidence_count, '' AS time_window,
                                   nrs.rank AS skill_rank
                            FROM normalized_role_skills nrs
                            JOIN normalized_skills ns ON ns.concept_id = nrs.concept_id
                            WHERE nrs.role_id IN ({placeholders})
                              AND nrs.final_score >= ? AND nrs.rank <= ?
                            ORDER BY nrs.role_id, nrs.rank
                            """,
                            values,
                        )
                    ]
            else:
                window_condition = "ss.time_window = ?" if time_window else "ss.window_start = (SELECT MAX(s2.window_start) FROM role_skill_snapshots s2 WHERE s2.role_id = ss.role_id)"
                values = [*role_ids]
                if time_window:
                    values.append(time_window)
                values.extend([min_support, max(1, min(skill_limit, 50))])
                skill_rows = [
                    dict(row)
                    for row in connection.execute(
                        f"""
                        WITH ranked AS (
                            SELECT ss.*, s.canonical_name, s.competency_category, s.tech_stack,
                                   ROW_NUMBER() OVER (PARTITION BY ss.role_id ORDER BY ss.adjusted_support DESC, ss.evidence_count DESC) AS skill_rank
                            FROM role_skill_snapshots ss JOIN skills s ON s.skill_id = ss.skill_id
                            WHERE ss.role_id IN ({placeholders}) AND {window_condition} AND ss.adjusted_support >= ?
                        )
                        SELECT * FROM ranked WHERE skill_rank <= ? ORDER BY role_id, skill_rank
                        """,
                        values,
                    )
                ]
            family_ids = sorted({row["family_id"] for row in role_rows if row["family_id"]})
            family_rows = []
            if family_ids:
                family_placeholders = ",".join("?" for _ in family_ids)
                family_rows = [dict(row) for row in connection.execute(f"SELECT * FROM role_families WHERE family_id IN ({family_placeholders})", family_ids)]
            quality = self._quality(connection)

        nodes: list[dict] = []
        edges: list[dict] = []
        domain_ids: set[str] = set()
        for family in family_rows:
            domain_id = f"domain:{family['domain_id']}"
            if domain_id not in domain_ids:
                nodes.append({"id": domain_id, "entity_id": family["domain_id"], "type": "domain", "label": family["domain_name"]})
                domain_ids.add(domain_id)
            family_node_id = f"family:{family['family_id']}"
            nodes.append({"id": family_node_id, "entity_id": family["family_id"], "type": "family", "label": family["family_name"]})
            edges.append({"id": f"domain-family:{family['family_id']}", "source": domain_id, "target": family_node_id, "relation": "HAS_FAMILY"})

        visible_role_ids = {row["role_id"] for row in role_rows}
        for row in role_rows:
            nodes.append({
                "id": row["role_id"], "entity_id": row["role_id"], "type": "role", "label": row["role_name"],
                "parent_role_id": row["parent_role_id"], "family_id": row["family_id"],
                "jd_count": row["document_count"], "company_count": row["company_count"], "focused": row["role_id"] == role_id,
            })
            if row["parent_role_id"] in visible_role_ids:
                source = row["parent_role_id"]
                relation = "HAS_SUBTYPE"
            else:
                source = f"family:{row['family_id']}"
                relation = "HAS_ROLE"
            edges.append({"id": f"role-tree:{source}:{row['role_id']}", "source": source, "target": row["role_id"], "relation": relation})

        categories: set[str] = set()
        for row in skill_rows:
            category_name = row["competency_category"] or "其他能力"
            category_id = f"category:{row['role_id']}:{category_name}"
            if category_id not in categories:
                nodes.append({"id": category_id, "entity_id": category_name, "role_id": row["role_id"], "type": "category", "label": category_name})
                edges.append({"id": f"role-category:{row['role_id']}:{category_name}", "source": row["role_id"], "target": category_id, "relation": "HAS_SKILL_GROUP"})
                categories.add(category_id)
            visual_skill_id = f"skill:{row['role_id']}:{row['skill_id']}"
            nodes.append({
                "id": visual_skill_id, "entity_id": row["skill_id"], "role_id": row["role_id"], "type": "skill",
                "label": row["canonical_name"], "stack": row["tech_stack"], "category": category_name,
                "support": row["adjusted_support"], "tier": row["tier"], "trend": row["trend"], "delta": row["delta"],
                "evidence_count": row["evidence_count"], "time_window": row["time_window"],
            })
            edges.append({
                "id": f"category-skill:{row['role_id']}:{row['skill_id']}", "source": category_id, "target": visual_skill_id,
                "relation": row["relation"], "tier": row["tier"], "support": row["adjusted_support"],
            })

        return {
            "nodes": nodes,
            "edges": edges,
            "related_edges": [],
            "stats": {
                "roles": len(role_rows), "skills": len(skill_rows), "categories": len(categories),
                "edges": len(edges),
                "evidence": sum(row["evidence_count"] for row in skill_rows),
            },
            "quality": quality,
            "available_windows": available_windows,
            "filters": {"time_window": time_window, "role_id": role_id, "min_support": min_support, "skill_limit": skill_limit},
        }

    def latest_profiles(self, role_id: str) -> dict:
        with self._connect() as connection:
            role = connection.execute("SELECT * FROM roles WHERE role_id = ?", (role_id,)).fetchone()
            if not role:
                raise KeyError("岗位不存在")
            profiles = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT p.* FROM role_profiles p
                    WHERE p.role_id = ? AND p.window_start = (
                        SELECT MAX(p2.window_start) FROM role_profiles p2
                        WHERE p2.role_id = p.role_id AND p2.industry_id = p.industry_id AND p2.level_id = p.level_id
                    ) ORDER BY p.industry_name, p.level_name
                    """,
                    (role_id,),
                )
            ]
            for profile in profiles:
                profile["skills"] = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT e.*, s.canonical_name, s.tech_stack, s.competency_category
                        FROM role_skill_edges e JOIN skills s ON s.skill_id = e.skill_id
                        WHERE e.profile_id = ? ORDER BY e.adjusted_support DESC
                        """,
                        (profile["profile_id"],),
                    )
                ]
            summary_skills = [
                dict(row)
                for row in connection.execute(
                    """
                    WITH latest AS (
                        SELECT p.* FROM role_profiles p
                        WHERE p.role_id = ? AND p.window_start = (
                            SELECT MAX(p2.window_start) FROM role_profiles p2 WHERE p2.role_id = p.role_id
                        )
                    )
                    SELECT s.skill_id, s.canonical_name, s.tech_stack, s.competency_category,
                           SUM(e.adjusted_support * p.jd_count) / SUM(p.jd_count) AS adjusted_support,
                           SUM(e.evidence_count) AS evidence_count
                    FROM latest p
                    JOIN role_skill_edges e ON e.profile_id = p.profile_id
                    JOIN skills s ON s.skill_id = e.skill_id
                    GROUP BY s.skill_id, s.canonical_name, s.tech_stack, s.competency_category
                    ORDER BY adjusted_support DESC, evidence_count DESC LIMIT 20
                    """,
                    (role_id,),
                )
            ]
            raw_titles = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT title, COUNT(*) AS amount FROM jds
                    WHERE role_id = ? AND duplicate_of = ''
                    GROUP BY title ORDER BY amount DESC, title LIMIT 8
                    """,
                    (role_id,),
                )
            ]
        return {"role": dict(role), "profiles": profiles, "summary_skills": summary_skills, "raw_titles": raw_titles}

    def evolution(self, role_id: str) -> list[dict]:
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT e.*, s.canonical_name, p.time_window AS current_window, p.level_name, p.industry_name
                    FROM evolution_edges e
                    JOIN role_profiles p ON p.profile_id = e.current_profile_id
                    JOIN skills s ON s.skill_id = e.skill_id
                    WHERE p.role_id = ? ORDER BY p.window_start, ABS(e.delta) DESC
                    """,
                    (role_id,),
                )
            ]

    def role_timeline(self, role_id: str) -> dict:
        with self._connect() as connection:
            role = connection.execute("SELECT * FROM roles WHERE role_id = ?", (role_id,)).fetchone()
            if not role:
                raise KeyError("岗位不存在")
            rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT ss.time_window, ss.window_start, ss.skill_id, s.canonical_name,
                           ss.adjusted_support, ss.previous_support, ss.delta, ss.trend,
                           ss.evidence_count, ss.tier
                    FROM role_skill_snapshots ss JOIN skills s ON s.skill_id = ss.skill_id
                    WHERE ss.role_id = ? ORDER BY ss.window_start, ss.adjusted_support DESC
                    """,
                    (role_id,),
                )
            ]
        windows: dict[str, dict] = {}
        for row in rows:
            window = windows.setdefault(row["time_window"], {"time_window": row["time_window"], "window_start": row["window_start"], "skills": []})
            if len(window["skills"]) < 12:
                window["skills"].append(row)
        return {"role": dict(role), "windows": list(windows.values())}

    def skill_evidence(
        self,
        skill_id: str,
        role_id: str = "",
        time_window: str = "",
        level: str = "",
        stack: str = "",
        limit: int = 50,
    ) -> dict:
        del level, stack
        with self._connect() as connection:
            skill = connection.execute("SELECT * FROM skills WHERE skill_id = ?", (skill_id,)).fetchone()
            normalized = False
            if not skill and connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'normalized_skills'"
            ).fetchone():
                skill = connection.execute(
                    "SELECT concept_id AS skill_id, canonical_name, category AS competency_category, '' AS tech_stack FROM normalized_skills WHERE concept_id = ?",
                    (skill_id,),
                ).fetchone()
                normalized = bool(skill)
            if not skill:
                raise KeyError("技能不存在")
            role = connection.execute("SELECT * FROM roles WHERE role_id = ?", (role_id,)).fetchone() if role_id else None
            if normalized:
                evidence_sql = """
                    SELECT e.*, j.title, j.canonical_role, j.company_name, j.posted_at, j.source_file,
                           j.description, j.tags, p.time_window
                    FROM normalized_evidence_map m
                    JOIN jd_skill_edges e ON e.jd_id = m.jd_id AND e.skill_id = m.original_skill_id
                    JOIN jds j ON j.jd_id = e.jd_id
                    LEFT JOIN role_profiles p ON p.profile_id = j.profile_id
                    WHERE m.concept_id = ? AND e.evidence_status IN ('VERIFIED', 'LOW_CONFIDENCE', 'ANALYSIS_ONLY')
                      AND (? = '' OR j.role_id = ?)
                      AND (? = '' OR p.time_window = ?)
                    ORDER BY CASE e.evidence_status WHEN 'VERIFIED' THEN 1 WHEN 'LOW_CONFIDENCE' THEN 2 ELSE 3 END,
                             e.confidence DESC, j.posted_at DESC LIMIT ?
                """
            else:
                evidence_sql = """
                    SELECT e.*, j.title, j.canonical_role, j.company_name, j.posted_at, j.source_file,
                           j.description, j.tags, p.time_window
                    FROM jd_skill_edges e
                    JOIN jds j ON j.jd_id = e.jd_id
                    LEFT JOIN role_profiles p ON p.profile_id = j.profile_id
                    WHERE e.skill_id = ? AND e.evidence_status IN ('VERIFIED', 'LOW_CONFIDENCE', 'ANALYSIS_ONLY')
                      AND (? = '' OR j.role_id = ?)
                      AND (? = '' OR p.time_window = ?)
                    ORDER BY CASE e.evidence_status WHEN 'VERIFIED' THEN 1 WHEN 'LOW_CONFIDENCE' THEN 2 ELSE 3 END,
                             e.confidence DESC, j.posted_at DESC LIMIT ?
                """
            evidence = [
                dict(row)
                for row in connection.execute(
                    evidence_sql,
                    (skill_id, role_id, role_id, time_window, time_window, max(1, min(limit, 200))),
                )
            ]
        return {"skill": dict(skill), "role": dict(role) if role else {}, "time_window": time_window, "evidence": evidence}

    def review_tasks(self, status: str = "PENDING", limit: int = 100) -> list[dict]:
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT t.*, j.title, j.company_name, j.source_file
                    FROM review_tasks t LEFT JOIN jds j ON j.jd_id = t.jd_id
                    WHERE (? = '' OR t.status = ?) ORDER BY t.confidence ASC LIMIT ?
                    """,
                    (status, status, max(1, min(limit, 500))),
                )
            ]

    def decide_review(self, task_id: str, status: str, decision: str) -> dict:
        normalized = status.upper()
        if normalized not in {"APPROVED", "REJECTED", "PENDING"}:
            raise ValueError("status 只能是 APPROVED、REJECTED 或 PENDING")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE review_tasks SET status = ?, decision = ? WHERE task_id = ?",
                (normalized, decision, task_id),
            )
            connection.commit()
            if cursor.rowcount == 0:
                raise KeyError("审核任务不存在")
            row = connection.execute("SELECT * FROM review_tasks WHERE task_id = ?", (task_id,)).fetchone()
        return dict(row)

    @staticmethod
    def _quality(connection: sqlite3.Connection) -> dict:
        row = connection.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN duplicate_of <> '' THEN 1 ELSE 0 END) AS duplicates,
                   SUM(CASE WHEN template_cluster_id <> '' THEN 1 ELSE 0 END) AS templates
            FROM jds
            """
        ).fetchone()
        evidence = {
            item["evidence_status"]: item["amount"]
            for item in connection.execute(
                "SELECT evidence_status, COUNT(*) AS amount FROM jd_skill_edges GROUP BY evidence_status"
            )
        }
        reviews = connection.execute("SELECT COUNT(*) FROM review_tasks WHERE status = 'PENDING'").fetchone()[0]
        return {
            "documents": row["total"] or 0,
            "duplicates": row["duplicates"] or 0,
            "template_documents": row["templates"] or 0,
            "evidence": evidence,
            "pending_reviews": reviews,
        }


def CounterLike(values) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[value] = result.get(value, 0) + 1
    return dict(sorted(result.items(), key=lambda item: (-item[1], item[0])))


class GraphRequestHandler(BaseHTTPRequestHandler):
    repository: GraphRepository
    page_path: Path

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        try:
            if path in {"/", "/index.html", "/panorama.html"}:
                self._send_page()
            elif path == "/api/health":
                self._json(self.repository.health())
            elif path == "/api/v1/facets":
                self._json(self.repository.facets())
            elif path == "/api/v1/industries":
                self._json(self.repository.facets()["industries"])
            elif path == "/api/v1/roles":
                self._json(self.repository.roles(_first(query, "industry"), _first(query, "level")))
            elif path == "/api/v1/graph/panorama":
                self._json(
                    self.repository.panorama(
                        industry=_first(query, "industry"),
                        level=_first(query, "level"),
                        stack=_first(query, "stack"),
                        category=_first(query, "category"),
                        time_window=_first(query, "time_window"),
                        role_id=_first(query, "role_id"),
                        min_support=_float(_first(query, "min_support"), 0.10),
                        skill_limit=_int(_first(query, "skill_limit"), 30),
                        role_limit=_int(_first(query, "role_limit"), 14),
                    )
                )
            elif path.startswith("/api/v1/roles/") and path.endswith("/profiles/latest"):
                role_id = path.removeprefix("/api/v1/roles/").removesuffix("/profiles/latest").strip("/")
                self._json(self.repository.latest_profiles(role_id))
            elif path.startswith("/api/v1/roles/") and path.endswith("/evolution"):
                role_id = path.removeprefix("/api/v1/roles/").removesuffix("/evolution").strip("/")
                self._json(self.repository.evolution(role_id))
            elif path.startswith("/api/v1/roles/") and path.endswith("/timeline"):
                role_id = path.removeprefix("/api/v1/roles/").removesuffix("/timeline").strip("/")
                self._json(self.repository.role_timeline(role_id))
            elif path.startswith("/api/v1/skills/") and path.endswith("/evidence"):
                skill_id = path.removeprefix("/api/v1/skills/").removesuffix("/evidence").strip("/")
                self._json(
                    self.repository.skill_evidence(
                        skill_id,
                        role_id=_first(query, "role_id"),
                        time_window=_first(query, "time_window"),
                        level=_first(query, "level"),
                        stack=_first(query, "stack"),
                        limit=_int(_first(query, "limit"), 50),
                    )
                )
            elif path == "/api/v1/review/tasks":
                self._json(self.repository.review_tasks(_first(query, "status", "PENDING"), _int(_first(query, "limit"), 100)))
            else:
                self._json({"error": "not_found", "path": path}, HTTPStatus.NOT_FOUND)
        except KeyError as error:
            self._json({"error": str(error)}, HTTPStatus.NOT_FOUND)
        except (ValueError, sqlite3.Error) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        try:
            if path.startswith("/api/v1/review/tasks/") and path.endswith("/decision"):
                task_id = path.removeprefix("/api/v1/review/tasks/").removesuffix("/decision").strip("/")
                length = _int(self.headers.get("Content-Length", "0"), 0)
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                self._json(
                    self.repository.decide_review(task_id, payload.get("status", "PENDING"), payload.get("decision", ""))
                )
            else:
                self._json({"error": "not_found", "path": path}, HTTPStatus.NOT_FOUND)
        except KeyError as error:
            self._json({"error": str(error)}, HTTPStatus.NOT_FOUND)
        except (ValueError, json.JSONDecodeError, sqlite3.Error) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def _send_page(self) -> None:
        if not self.page_path.exists():
            self._json({"error": "可视化页面不存在"}, HTTPStatus.NOT_FOUND)
            return
        payload = self.page_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, value, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(payload)

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, format: str, *args) -> None:
        print(f"[API] {self.address_string()} - {format % args}")


def _first(query: dict, key: str, default: str = "") -> str:
    return str(query.get(key, [default])[0])


def _float(value: str, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def create_repository(
    database_path: Path,
    backend: str = "auto",
    neo4j_config: Path | None = None,
):
    normalized_backend = backend.strip().lower()
    if normalized_backend not in {"auto", "sqlite", "neo4j"}:
        raise ValueError(f"不支持的数据源：{backend}")
    connection_path = (neo4j_config or database_path.parent / "neo4j_connection.json").resolve()
    if normalized_backend == "neo4j" or (normalized_backend == "auto" and connection_path.exists()):
        if not connection_path.exists():
            raise FileNotFoundError(f"Neo4j连接配置不存在：{connection_path}")
        from .neo4j_repository import Neo4jGraphRepository

        neo4j_repository = Neo4jGraphRepository(connection_path)
        neo4j_repository.health()
        return neo4j_repository
    if not database_path.exists():
        raise FileNotFoundError(f"SQLite数据库不存在：{database_path}")
    return GraphRepository(database_path)


def run_server(
    database_path: Path,
    page_path: Path,
    host: str = "127.0.0.1",
    port: int = 8010,
    backend: str = "auto",
    neo4j_config: Path | None = None,
) -> None:
    repository = create_repository(database_path, backend, neo4j_config)
    backend = repository.health().get("backend", "sqlite")
    handler = type(
        "ConfiguredGraphRequestHandler",
        (GraphRequestHandler,),
        {"repository": repository, "page_path": page_path},
    )
    server = ThreadingHTTPServer((host, port), handler)
    print(f"可信岗位图谱 API 已启动：http://{host}:{port}")
    print(f"数据源：{backend}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
