from __future__ import annotations

from scripts.backfill_normalized_skill_stacks import choose_stack
from trusted_graph_agent.neo4j_filtered_view import _published_panorama, load_facets


def test_facets_read_stacks_from_active_normalized_graph() -> None:
    class FakeClient:
        def query(self, statement, parameters=None):
            if "NormalizationPointer" in statement:
                return [{"run_id": "normalization:test"}]
            if "Industry" in statement:
                return []
            if "AT_LEVEL" in statement:
                return []
            if "HAS_CORE_SKILL" in statement and "tech_stack" in statement:
                assert parameters == {"run_id": "normalization:test"}
                return [{"tech_stack": "后端"}, {"tech_stack": "AI"}]
            if "RoleFamily" in statement:
                return []
            if "RoleProfile" in statement:
                return []
            if "s.category" in statement:
                return []
            raise AssertionError(statement)

    assert load_facets(FakeClient())["stacks"] == ["后端", "AI"]


def test_stack_backfill_prefers_existing_then_mapped_candidates() -> None:
    assert choose_stack(
        {"tech_stack": "数据库", "canonical_name": "MySQL", "ability_stacks": []}, {}, {}
    ) == ("数据库", "existing")
    assert choose_stack(
        {"tech_stack": "", "canonical_name": "新技能", "ability_stacks": ["后端", "后端", "AI"]}, {}, {}
    ) == ("后端", "ability_or_legacy")


def test_published_node_exposes_normalized_stack() -> None:
    class FakeClient:
        def query(self, statement, parameters=None):
            assert parameters.get("stack") == "后端"
            return [{
                "skill_id": "skill:1", "canonical_name": "Python",
                "competency_category": "技术", "tech_stack": "后端",
                "adjusted_support": 0.8, "evidence_count": 3, "skill_rank": 1,
            }]

    result = _published_panorama(
        FakeClient(), {"role_id": "role:1", "role_name": "后端开发", "company_count": 1},
        "normalization:test", "", 0, 10, [],
        stack="后端",
    )
    node = next(item for item in result["nodes"] if item["type"] == "skill")
    assert node["stack"] == "后端"
