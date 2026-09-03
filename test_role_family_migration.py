from __future__ import annotations

import pytest

from scripts.reclassify_role_families import plan_updates
from scripts.reclassify_role_families import run


class FakeClient:
    def __init__(self, roles):
        self.roles = roles
        self.writes = []

    def query(self, statement, parameters=None, access_mode="Read"):
        if "exactly_one_family" in statement and access_mode == "Read":
            return [{
                "roles": len(self.roles),
                "exactly_one_family": len(self.roles),
                "fallback_roles": 0,
                "fallback_family_nodes": 0,
                "violations": [],
            }]
        if "MATCH (role:Role)" in statement and access_mode == "Read":
            return self.roles
        self.writes.append((statement, parameters, access_mode))
        return []


class FakeRepository:
    def __init__(self, roles):
        self.client = FakeClient(roles)


def test_role_family_migration_reclassifies_and_rewires() -> None:
    repository = FakeRepository([
        {
            "role_id": "role:solution",
            "role_name": "解决方案工程师",
            "current_family_id": "extended_roles",
        },
        {
            "role_id": "role:ai-deploy",
            "role_name": "AI部署工程师",
            "current_family_id": "extended_roles",
        },
    ])

    result = run(repository, apply=True)

    assert result["status"] == "APPLIED"
    assert result["unresolved_roles"] == []
    assert result["target_family_counts"] == {
        "ai": 1,
        "enterprise_solutions": 1,
    }
    statements = "\n".join(row[0] for row in repository.client.writes)
    assert "DELETE old" in statements
    assert "family_id:'extended_roles'" in statements


def test_role_family_migration_refuses_partial_apply() -> None:
    repository = FakeRepository([
        {
            "role_id": "role:unknown",
            "role_name": "尚未分类的新岗位",
            "current_family_id": "extended_roles",
        }
    ])

    with pytest.raises(ValueError, match="迁移已拒绝执行"):
        run(repository, apply=True)

    assert repository.client.writes == []


def test_taxonomy_aliases_are_available_to_migration() -> None:
    updates, unresolved = plan_updates([
        {"role_id": "role:fae", "role_name": "FAE现场应用工程师"},
        {"role_id": "role:frontend", "role_name": "Web前端开发"},
    ])

    assert unresolved == []
    assert [row["family_id"] for row in updates] == [
        "enterprise_solutions",
        "client",
    ]
