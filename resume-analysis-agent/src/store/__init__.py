"""数据抽象层 — 依赖倒置。核心逻辑不 import neo4j。"""

from .interface import RoleStore
from .memory_store import MemoryRoleStore
from .neo4j_store import Neo4jRoleStore

__all__ = ["RoleStore", "MemoryRoleStore", "Neo4jRoleStore"]


def create_store(backend: str = "memory") -> RoleStore:
    """按后端名创建 RoleStore 实现。

    Args:
        backend: "neo4j"（需 NEO4J_PASSWORD）或 "memory"（内嵌示例数据，默认）。

    Returns:
        RoleStore 实例。
    """
    if backend == "neo4j":
        return Neo4jRoleStore()
    if backend == "memory":
        return MemoryRoleStore()
    raise ValueError(f"未知 STORE_BACKEND: {backend}（可选: memory / neo4j）")

