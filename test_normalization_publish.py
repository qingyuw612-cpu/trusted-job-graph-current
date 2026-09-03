from __future__ import annotations

import csv
import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from processing_layer.publish_normalization import NormalizationPublisher
from processing_layer.publish_normalization import load_snapshot
from trusted_graph_agent.neo4j_filtered_view import (
    _published_panorama,
    _published_time_panorama,
)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class NormalizationPublishTests(unittest.TestCase):
    def test_snapshot_is_deterministic_and_validated(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "knowledge_graph.db"
            reports = root / "reports"
            reports.mkdir()
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "CREATE TABLE jds (jd_id TEXT, canonical_role TEXT, company_id TEXT, duplicate_of TEXT)"
                )
                connection.executemany(
                    "INSERT INTO jds VALUES (?, ?, ?, '')",
                    [("jd:1", "数据分析师", "company:1"), ("jd:2", "数据分析师", "company:2")],
                )
                connection.commit()
            finally:
                connection.close()
            write_csv(
                reports / "normalized_concepts.csv",
                [
                    "concept_id",
                    "canonical_name",
                    "category",
                    "status",
                    "source_phrase_count",
                    "jd_count",
                    "company_count",
                    "verified_rate",
                ],
                [
                    {
                        "concept_id": "skill:1",
                        "canonical_name": "数据分析",
                        "category": "技术",
                        "status": "STANDARD",
                        "source_phrase_count": 1,
                        "jd_count": 2,
                        "company_count": 2,
                        "verified_rate": 1,
                    }
                ],
            )
            write_csv(
                reports / "role_top_skills.csv",
                [
                    "role",
                    "concept_id",
                    "canonical_name",
                    "final_score",
                    "company_count",
                    "jd_count",
                    "verified_jd_count",
                    "minimum_verified_jd_count",
                    "mmr_rank",
                ],
                [
                    {
                        "role": "数据分析师",
                        "concept_id": "skill:1",
                        "canonical_name": "数据分析",
                        "final_score": 0.5,
                        "company_count": 2,
                        "jd_count": 2,
                        "verified_jd_count": 2,
                        "minimum_verified_jd_count": 2,
                        "mmr_rank": 1,
                    }
                ],
            )
            write_csv(
                reports / "skill_normalization_mapping.csv",
                ["source_name", "concept_id"],
                [{"source_name": "数据分析能力", "concept_id": "skill:1"}],
            )
            (reports / "normalization_report.json").write_text(
                json.dumps({"status": "ok"}), encoding="utf-8"
            )
            first = load_snapshot(database, reports)
            second = load_snapshot(database, reports)
            self.assertEqual(first.run_id, second.run_id)
            self.assertEqual(
                first.expected,
                {"roles": 1, "concepts": 1, "core_edges": 1, "mapping_names": 1},
            )
            self.assertEqual(first.roles[0]["document_count"], 2)
            self.assertEqual(first.roles[0]["family_id"], "data")

            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "UPDATE jds SET canonical_role = '测试岗位'"
                )
                connection.commit()
            finally:
                connection.close()
            rows = list(csv.DictReader(
                (reports / "role_top_skills.csv").open(
                    "r", encoding="utf-8-sig", newline=""
                )
            ))
            rows[0]["role"] = "测试岗位"
            write_csv(
                reports / "role_top_skills.csv",
                list(rows[0]),
                rows,
            )
            with self.assertRaisesRegex(ValueError, "已拒绝发布"):
                load_snapshot(database, reports)

    def test_publish_replaces_old_family_edge_and_removes_empty_fallback(self) -> None:
        class CapturingPublisher(NormalizationPublisher):
            def __init__(self):
                self.batch_size = 500
                self.statements = []

            def _ensure_schema(self):
                return None

            def query(self, statement, parameters=None, *, write=False, attempts=4):
                self.statements.append(statement)
                if "RETURN count(edge) AS mapped" in statement:
                    return [{"mapped": 1}]
                return []

            def verify(self, run_id):
                return {"roles": 1, "core_edges": 1, "concepts": 1, "mapping_edges": 1}

        publisher = CapturingPublisher()
        snapshot = type("Snapshot", (), {
            "run_id": "normalization:test",
            "created_at": "2026-09-01T00:00:00+00:00",
            "source_database": Path("db.sqlite"),
            "normalization_dir": Path("reports"),
            "roles": [{"role_id": "role:1"}],
            "concepts": [{"concept_id": "skill:1"}],
            "core_edges": [{"role_id": "role:1", "concept_id": "skill:1"}],
            "mappings": [{"normalized_name": "数据分析", "concept_id": "skill:1"}],
            "expected": {"roles": 1, "concepts": 1, "core_edges": 1, "mapping_names": 1},
        })()

        publisher.publish(snapshot)

        statements = "\n".join(publisher.statements)
        self.assertIn("OPTIONAL MATCH (previous_family:RoleFamily)-[old:HAS_ROLE]->(role)", statements)
        self.assertIn("DELETE old", statements)
        self.assertIn("family_id:'extended_roles'", statements)

    def test_published_panorama_uses_core_edges(self) -> None:
        class FakeClient:
            def query(self, statement, parameters=None):
                self.parameters = parameters
                return [
                    {
                        "skill_id": "skill:1",
                        "canonical_name": "数据分析",
                        "competency_category": "技术",
                        "adjusted_support": 0.5,
                        "evidence_count": 20,
                        "skill_rank": 1,
                    }
                ]

        result = _published_panorama(
            FakeClient(),
            {
                "role_id": "role:1",
                "role_name": "测试岗位",
                "family_id": "extended_roles",
                "family_name": "扩展岗位",
                "domain_id": "multi_industry",
                "domain_name": "多行业岗位",
                "document_count": 100,
                "company_count": 50,
            },
            "normalization:test",
            "",
            0.1,
            30,
            ["2025Q2", "2025Q3"],
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["stats"]["skills"], 1)
        self.assertEqual(result["quality"]["normalization_run_id"], "normalization:test")
        self.assertEqual(result["available_windows"], ["2025Q2", "2025Q3"])

    def test_published_time_panorama_uses_filtered_raw_quarter(self) -> None:
        class FakeClient:
            def __init__(self):
                self.calls = 0

            def query(self, statement, parameters=None):
                self.calls += 1
                self.statement = statement
                self.parameters = parameters
                if self.calls == 1:
                    return [{"total_jds": 10}]
                return [
                    {
                        "skill_id": "skill:1",
                        "canonical_name": "数据分析",
                        "competency_category": "技术",
                        "tech_stack": "Python",
                        "adjusted_support": 0.5,
                        "evidence_count": 8,
                    }
                ]

        result = _published_time_panorama(
            FakeClient(),
            {
                "role_id": "role:1",
                "role_name": "数据分析师",
                "company_count": 6,
            },
            "normalization:test",
            "",
            "2025Q2",
            "",
            0.1,
            30,
            ["2025Q1", "2025Q2"],
        )
        self.assertEqual(result["stats"]["filtered_jds"], 10)
        self.assertEqual(result["stats"]["skills"], 1)
        self.assertEqual(result["filters"]["effective_time_window"], "2025Q2")
        self.assertEqual(result["nodes"][-1]["time_window"], "2025Q2")


if __name__ == "__main__":
    unittest.main()
