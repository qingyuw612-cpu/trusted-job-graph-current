from __future__ import annotations

import csv
import copy
import json
import tempfile
import unittest
from pathlib import Path

from raw_jd_layer.importer import (
    BATCH_QUERY,
    FileContext,
    file_context,
    iter_records,
    prepare_row,
    stable_hash,
)


class RawJDImportTests(unittest.TestCase):
    def context(self) -> FileContext:
        return FileContext(
            relative_path="猎聘/Java.json",
            source_file_id="rawsource:test",
            file_signature="1:1",
            file_mtime_epoch=1_700_000_000,
            declared_role="Java",
            source_category="后端开发",
            default_platform="历史数据",
        )

    def test_same_job_content_change_creates_version_not_new_job(self) -> None:
        first = prepare_row(
            {"job_id": "123", "job_name": "Java", "jd": "熟悉Spring", "company": "公司A", "source": "猎聘"},
            self.context(),
            "2026-01-01T00:00:00+00:00",
        )
        second = prepare_row(
            {"job_id": "123", "job_name": "Java", "jd": "熟悉Spring Boot", "company": "公司A", "source": "猎聘"},
            self.context(),
            "2026-01-02T00:00:00+00:00",
        )
        self.assertEqual(first["raw_uid"], second["raw_uid"])
        self.assertNotEqual(first["version_id"], second["version_id"])

    def test_collection_and_processing_metadata_do_not_create_version(self) -> None:
        original = {
            "job_id": "123",
            "job_name": "Java",
            "jd": "熟悉 Spring Boot",
            "company": "公司A",
            "source": "猎聘",
            "publish_time": "2026-08-01",
            "collected_at": "2026-08-02",
            "job_link": "https://example.test/job/123?a=1",
            "search_keyword": "Java",
        }
        changed = copy.deepcopy(original)
        changed.update(
            {
                "collected_at": "2026-08-14",
                "job_link": "https://example.test/job/123?a=2",
                "search_keyword": "后端开发",
                "能力提取结果": "技能：Java；知识：分布式系统",
            }
        )
        first = prepare_row(original, self.context(), "2026-08-14T00:00:00+00:00")
        second = prepare_row(changed, self.context(), "2026-08-14T00:00:00+00:00")
        self.assertEqual(first["version_id"], second["version_id"])

    def test_import_reuses_legacy_version_with_same_business_content(self) -> None:
        self.assertIn("OPTIONAL MATCH (job)-[:HAS_VERSION]->(matching:RawJDVersion)", BATCH_QUERY)
        self.assertIn("head(collect(matching)) AS existing", BATCH_QUERY)
        self.assertNotIn("MERGE (version)-[:FROM_SOURCE]->(source)", BATCH_QUERY)
        self.assertNotIn("MERGE (job)-[:SEEN_IN_SOURCE]->(source)", BATCH_QUERY)

    def test_csv_and_json_array_streaming(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            csv_path = root / "jobs.csv"
            with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["jobID", "职位名称", "职位描述"])
                writer.writeheader()
                writer.writerow({"jobID": "1", "职位名称": "Python", "职位描述": "负责开发"})
            json_path = root / "jobs.json"
            json_path.write_text(
                json.dumps([{"job_id": "2", "job_name": "Java", "jd": "负责后端"}], ensure_ascii=False),
                encoding="utf-8",
            )
            self.assertEqual(len(list(iter_records(csv_path))), 1)
            self.assertEqual(len(list(iter_records(json_path))), 1)

    def test_51job_2026_fields_are_mapped_without_trusting_search_category(self) -> None:
        context = FileContext(
            relative_path="jobs_2026_it.csv",
            source_file_id="rawsource:51job-test",
            file_signature="1:1",
            file_mtime_epoch=1_785_427_200,
            declared_role="jobs_2026_it",
            source_category="",
            default_platform="前程无忧",
        )
        row = prepare_row(
            {
                "职位ID": "164355725",
                "岗位关键词": "PHP开发工程师",
                "原始职位名称": "开发工程师",
                "JD全文": "负责 Java 与 Spring Boot 后端开发",
                "最低工资": "12000",
                "最高工资": "16000",
                "公司全称": "示例公司",
                "公司行业": "计算机软件",
                "工作经验": "3年",
                "发布日期": "2026-05-06",
                "职位详情链接": "https://jobs.51job.com/example/164355725.html",
                "搜索城市": "北京",
                "搜索关键词": "PHP开发工程师",
                "采集时间": "2026-07-28T18:45:35",
            },
            context,
            "2026-08-01T00:00:00+00:00",
        )
        props = row["version_props"]
        self.assertEqual(row["job_props"]["source_platform"], "前程无忧")
        self.assertEqual(row["job_props"]["source_job_id"], "164355725")
        self.assertEqual(props["title"], "开发工程师")
        self.assertEqual(props["description"], "负责 Java 与 Spring Boot 后端开发")
        self.assertEqual(props["declared_role"], "PHP开发工程师")
        self.assertEqual(props["declared_role_trust"], "SEARCH_CATEGORY")
        self.assertEqual(props["salary"], "12000-16000")
        self.assertTrue(props["company_id"].startswith("sourcecompany:"))
        self.assertEqual(props["publish_time_raw"], "2026-05-06")
        self.assertEqual(props["observed_at_raw"], "2026-07-28T18:45:35")

    def test_processed_51job_fields_reuse_ability_and_normalized_role(self) -> None:
        row = prepare_row(
            {
                "职位ID": "164355725",
                "岗位关键词": "PHP开发工程师",
                "原始职位名称": "开发工程师",
                "JD全文": "负责 Java 与 Spring Boot 后端开发",
                "能力提取结果": '{"技术":"Java、Spring Boot"}',
                "岗位名称": "开发工程师",
            },
            self.context(),
            "2026-08-01T00:00:00+00:00",
        )
        props = row["version_props"]
        self.assertEqual(props["ability_analysis_raw"], '{"技术":"Java、Spring Boot"}')
        self.assertEqual(props["declared_role"], "开发工程师")
        self.assertEqual(props["declared_role_trust"], "PROCESSED_NORMALIZATION")
        self.assertEqual(props["source_normalized_role"], "开发工程师")

    def test_source_namespace_prevents_cross_platform_filename_collision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "jobs.csv"
            path.write_text("title,description\nA,B\n", encoding="utf-8")
            first = file_context(path, root, "平台一", "platform-one")
            second = file_context(path, root, "平台二", "platform-two")
            legacy = file_context(path, root, "历史平台")
            expected_legacy = "rawsource:" + stable_hash("jobs.csv", 40)
            self.assertNotEqual(first.source_file_id, second.source_file_id)
            self.assertNotEqual(first.source_file_id, legacy.source_file_id)
            self.assertEqual(expected_legacy, legacy.source_file_id)


if __name__ == "__main__":
    unittest.main()
