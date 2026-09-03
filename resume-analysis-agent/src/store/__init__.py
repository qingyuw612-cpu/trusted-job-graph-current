"""数据抽象层 — 依赖倒置。核心逻辑不 import neo4j。"""

from __future__ import annotations

import atexit
import os
import threading

from .interface import RoleStore
from .memory_store import MemoryRoleStore
from .neo4j_store import Neo4jRoleStore

__all__ = [
    "RoleStore",
    "MemoryRoleStore",
    "Neo4jRoleStore",
    "create_store",
    "get_shared_store",
]


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


_SHARED_STORES: dict[str, RoleStore] = {}
_SHARED_STORES_LOCK = threading.Lock()


def get_shared_store(backend: str | None = None) -> RoleStore:
    """返回进程级共享 Store；未指定时读取 ``STORE_BACKEND``。"""
    normalized = (backend or os.getenv("STORE_BACKEND", "memory")).strip().lower()
    with _SHARED_STORES_LOCK:
        store = _SHARED_STORES.get(normalized)
        if store is None:
            store = create_store(normalized)
            _SHARED_STORES[normalized] = store
        return store


def _close_shared_stores() -> None:
    """进程退出时释放支持 ``close`` 的 Store（主要是 Neo4j Driver）。"""
    with _SHARED_STORES_LOCK:
        stores = list(_SHARED_STORES.values())
        _SHARED_STORES.clear()
    for store in stores:
        close = getattr(store, "close", None)
        if callable(close):
            close()


atexit.register(_close_shared_stores)
