"""数据抽象层测试 — memory 后端与工厂。"""
import os

import pytest

from src.store import MemoryRoleStore, create_store
from src.store.memory_store import _SAMPLE_ROLES


class TestMemoryStore:
    def test_get_all_roles(self):
        store = MemoryRoleStore()
        roles = store.get_all_roles()
        assert len(roles) == len(_SAMPLE_ROLES)
        assert roles[0]["role_name"]
        assert roles[0]["skills"]
        assert roles[0]["jd_count"] > 0

    def test_get_role_by_name(self):
        store = MemoryRoleStore()
        role = store.get_role_by_name("大模型算法工程师")
        assert role is not None
        assert role["role_name"] == "大模型算法工程师"

    def test_unknown_role_returns_none(self):
        store = MemoryRoleStore()
        assert store.get_role_by_name("不存在的岗位") is None


class TestFactory:
    def test_memory_backend(self):
        assert isinstance(create_store("memory"), MemoryRoleStore)

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError):
            create_store("unknown")

    def test_neo4j_without_password_raises(self):
        # 显式置空，避免被 .env 覆盖
        os.environ["NEO4J_PASSWORD"] = ""
        with pytest.raises(ValueError):
            create_store("neo4j")

