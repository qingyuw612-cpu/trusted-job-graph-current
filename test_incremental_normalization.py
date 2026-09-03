from __future__ import annotations

from processing_layer.incremental_normalization import IncrementalNormalizationPublisher
from trusted_graph_agent.normalization_experiment import NormalizationConfig
from trusted_graph_agent.text_utils import stable_id


class FakeClient:
    def __init__(self, *, roles=None):
        self.roles = roles or []

    def query(self, statement, parameters=None, access_mode="Read"):
        del parameters, access_mode
        if "algorithm_version:$algorithm" in statement:
            return []
        if "NormalizationPointer" in statement and "RETURN run.run_id" in statement:
            return [{"run_id": "normalization:base"}]
        if "RETURN DISTINCT raw.domain_role AS role_name" in statement:
            return [{"role_name": role} for role in self.roles]
        raise AssertionError(statement)


class FakeRepository:
    def __init__(self, client):
        self.client = client


def test_no_affected_roles_is_a_successful_noop():
    publisher = IncrementalNormalizationPublisher(
        FakeRepository(FakeClient()), NormalizationConfig()
    )
    result = publisher.run(["rawrun:1"], publish=True)
    assert result == {"status": "NO_CHANGES", "ingest_run_ids": ["rawrun:1"]}


def test_read_only_plan_identifies_only_affected_roles():
    publisher = IncrementalNormalizationPublisher(
        FakeRepository(FakeClient(roles=["Java开发工程师"])), NormalizationConfig()
    )
    result = publisher.run(["rawrun:1"], publish=False)
    assert result["status"] == "READY"
    assert result["affected_roles"] == [
        {
            "role_name": "Java开发工程师",
            "role_id": stable_id("role", "Java开发工程师"),
        }
    ]


def test_lightweight_delta_preserves_published_role_core_edges():
    class RecordingPublisher(IncrementalNormalizationPublisher):
        def __init__(self):
            super().__init__(FakeRepository(FakeClient()), NormalizationConfig())
            self.writes = []

        def query(self, statement, parameters=None, *, write=False):
            if write:
                self.writes.append(statement)
            if "role_count" in statement:
                return [{"role_count": 84}]
            if "baseline_jd_total" in statement:
                return [{"jd_total": 3, "company_total": 3, "baseline_jd_total": 4725}]
            raise AssertionError(statement)

    publisher = RecordingPublisher()
    updated = publisher.recompute_roles(
        "normalization:inc:test",
        [{"role_name": "Java开发工程师", "role_id": stable_id("role", "Java开发工程师")}],
    )

    assert updated == 0
    assert publisher.writes == []
