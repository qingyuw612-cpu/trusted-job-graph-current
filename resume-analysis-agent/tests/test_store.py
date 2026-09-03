"""数据抽象层测试 — memory 后端与工厂。"""
import os

import pytest

from src.store import MemoryRoleStore, Neo4jRoleStore, create_store
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


class TestNeo4jStoreCache:
    def test_reuses_driver_and_role_cache(self, monkeypatch):
        records = [
            {
                "role_name": "数据分析师",
                "family_name": "数据",
                "domain_name": "数字技术",
                "jd_cnt": 123,
                "skills": [
                    {"name": "Python", "category": "技术", "weight": 0.9, "rank": 1}
                ],
            }
        ]

        class FakeSession:
            def __init__(self, driver):
                self.driver = driver

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def run(self, cypher):
                assert "OPTIONAL MATCH (jd:JD)" not in cypher
                self.driver.run_count += 1
                return records

        class FakeDriver:
            def __init__(self):
                self.run_count = 0
                self.closed = False

            def session(self, *, database):
                assert database == "neo4j"
                return FakeSession(self)

            def close(self):
                self.closed = True

        driver = FakeDriver()
        monkeypatch.setenv("NEO4J_PASSWORD", "test-password")
        monkeypatch.setenv("ROLE_CACHE_TTL_SECONDS", "300")
        monkeypatch.setattr("neo4j.GraphDatabase.driver", lambda *_, **__: driver)

        store = Neo4jRoleStore()
        first = store.get_all_roles()
        second = store.get_all_roles()

        assert first is second
        assert store.get_role_by_name("数据分析师") is first[0]
        assert driver.run_count == 1

        store.invalidate_cache()
        store.get_all_roles()
        assert driver.run_count == 2

        store.close()
        assert driver.closed is True
