"""Neo4j RoleStore 实现 — 从 resume-handoff 图谱加载 Role 及核心技能。"""

import os
import threading
import time
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
        from neo4j import GraphDatabase

        self.uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        self.user = os.getenv("NEO4J_USER", "neo4j")
        self.password = os.getenv("NEO4J_PASSWORD", "")
        self.database = os.getenv("NEO4J_DATABASE", "neo4j")
        if not self.password:
            raise ValueError(
                "未配置 NEO4J_PASSWORD 环境变量。请配置后重试，"
                "或使用 STORE_BACKEND=memory 内嵌示例数据。"
            )
        self.cache_ttl_seconds = max(
            0.0, float(os.getenv("ROLE_CACHE_TTL_SECONDS", "300"))
        )
        self._driver = GraphDatabase.driver(
            self.uri,
            auth=(self.user, self.password),
        )
        self._cache_lock = threading.RLock()
        self._roles_cache: List[Dict[str, Any]] | None = None
        self._roles_by_name: Dict[str, Dict[str, Any]] = {}
        self._cache_loaded_at = 0.0

    def get_all_roles(self) -> List[Dict[str, Any]]:
        """返回岗位画像；缓存命中时不访问 Neo4j。"""
        with self._cache_lock:
            now = time.monotonic()
            if (
                self._roles_cache is not None
                and now - self._cache_loaded_at < self.cache_ttl_seconds
            ):
                return self._roles_cache

            roles = self._load_roles()
            self._roles_cache = roles
            self._roles_by_name = {
                str(role.get("role_name") or ""): role for role in roles
            }
            self._cache_loaded_at = now
            return roles

    def _load_roles(self) -> List[Dict[str, Any]]:
        """从 Neo4j 加载一份岗位—核心技能快照。"""

        cypher = """
            MATCH (role:Role)-[edge:HAS_CORE_SKILL]->(skill:NormalizedSkill)
            WITH role, edge, skill
            ORDER BY coalesce(role.role_name, role.name), edge.rank
            RETURN coalesce(role.role_name, role.name) AS role_name,
                   role.family_name AS family_name,
                   role.domain_name AS domain_name,
                   coalesce(role.document_count, 0) AS jd_cnt,
                   collect({
                       name: skill.canonical_name,
                       category: skill.category,
                       weight: coalesce(edge.final_score, 0.0),
                       rank: coalesce(edge.rank, 9999)
                   }) AS skills
        """

        roles: List[Dict[str, Any]] = []
        with self._driver.session(database=self.database) as session:
            result = session.run(cypher)
            for record in result:
                roles.append(self._build_role_entry(record))

        if not roles:
            raise RuntimeError("Neo4j 中未找到 Role-HAS_CORE_SKILL 数据，请检查图谱是否完整。")
        return roles

    def get_role_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        self.get_all_roles()
        return self._roles_by_name.get(name)

    def invalidate_cache(self) -> None:
        """使岗位画像缓存失效；下次读取时从 Neo4j 重载。"""
        with self._cache_lock:
            self._roles_cache = None
            self._roles_by_name = {}
            self._cache_loaded_at = 0.0

    def close(self) -> None:
        """关闭进程级 Neo4j Driver 及其连接池。"""
        self._driver.close()

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
