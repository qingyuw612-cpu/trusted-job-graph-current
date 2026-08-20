"""Neo4j RoleStore 实现 — 从 resume-handoff 图谱加载 Role 及核心技能。"""

import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from .interface import RoleStore

load_dotenv()


class Neo4jRoleStore(RoleStore):
    """从 Neo4j 图谱加载 Role-HAS_CORE_SKILL 数据。

    图谱结构（resume-handoff.dump）：
        Role -[:HAS_CORE_SKILL {final_score, rank}]-> NormalizedSkill
        JD   -[:INSTANCE_OF]-> Role
    """

    def __init__(self) -> None:
        self.uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        self.user = os.getenv("NEO4J_USER", "neo4j")
        self.password = os.getenv("NEO4J_PASSWORD", "")
        self.database = os.getenv("NEO4J_DATABASE", "neo4j")
        if not self.password:
            raise ValueError(
                "未配置 NEO4J_PASSWORD 环境变量。请配置后重试，"
                "或使用 STORE_BACKEND=memory 内嵌示例数据。"
            )

    def get_all_roles(self) -> List[Dict[str, Any]]:
        from neo4j import GraphDatabase

        cypher = """
            MATCH (role:Role)-[edge:HAS_CORE_SKILL]->(skill:NormalizedSkill)
            OPTIONAL MATCH (jd:JD)-[:INSTANCE_OF]->(role)
            WITH role, edge, skill, count(DISTINCT jd) AS jd_cnt
            ORDER BY role.role_name, edge.rank
            RETURN role.role_name AS role_name,
                   role.family_name AS family_name,
                   role.domain_name AS domain_name,
                   jd_cnt,
                   collect({
                       name: skill.canonical_name,
                       category: skill.category,
                       weight: coalesce(edge.final_score, 0.0),
                       rank: coalesce(edge.rank, 9999)
                   }) AS skills
        """

        driver = GraphDatabase.driver(self.uri, auth=(self.user, self.password))
        roles: List[Dict[str, Any]] = []
        try:
            with driver.session(database=self.database) as session:
                result = session.run(cypher)
                for record in result:
                    roles.append(self._build_role_entry(record))
        finally:
            driver.close()

        if not roles:
            raise RuntimeError("Neo4j 中未找到 Role-HAS_CORE_SKILL 数据，请检查图谱是否完整。")
        return roles

    def get_role_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        for role in self.get_all_roles():
            if role.get("role_name") == name:
                return role
        return None

    @staticmethod
    def _build_role_entry(record) -> Dict[str, Any]:
        skills = []
        for item in record.get("skills", []) or []:
            skills.append(
                {
                    "name": str(item.get("name", "")).strip(),
                    "category": str(item.get("category", "")).strip(),
                    "weight": float(item.get("weight", 0.0) or 0.0),
                    "rank": int(item.get("rank", 9999) or 9999),
                }
            )
        return {
            "role_name": record.get("role_name", "Unknown"),
            "family_name": record.get("family_name", ""),
            "domain_name": record.get("domain_name", ""),
            "jd_count": record.get("jd_cnt", 0),
            "skills": skills,
        }

