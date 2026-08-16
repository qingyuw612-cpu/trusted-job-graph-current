from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path

from extract_five_dimension_abilities import (
    JsonlCache,
    extract_json_object,
    process_record,
    validate_result,
)
from trusted_graph_agent.extractors import AbilityAnalysisExtractor
from trusted_graph_agent.models import JobDocument
from trusted_graph_agent.registry import SkillRegistry


class FiveDimensionExtractionTests(unittest.TestCase):
    def test_result_keeps_only_original_evidence(self) -> None:
        source = "负责 Python 和 Spring Boot 开发，具备团队协作和责任心，主动学习新技术。"
        result = validate_result(
            {
                "知识": ["软件工程"],
                "技术": ["Python", "Spring Boot"],
                "动机": ["主动学习"],
                "特质": ["团队协作"],
                "自我概念": ["责任心"],
            },
            source,
        )
        self.assertEqual([], result["知识"])
        self.assertEqual(["Python", "Spring Boot"], result["技术"])
        self.assertEqual(["主动学习"], result["动机"])
        self.assertEqual(["团队协作"], result["特质"])
        self.assertEqual(["责任心"], result["自我概念"])

    def test_parser_accepts_markdown_wrapped_json(self) -> None:
        result = extract_json_object('```json\n{"知识": [], "技术": ["SQL"]}\n```')
        self.assertEqual(["SQL"], result["技术"])

    def test_empty_dimensions_are_always_present(self) -> None:
        result = validate_result({"技术": "Python、SQL"}, "熟练使用 Python 和 SQL")
        self.assertEqual(["Python", "SQL"], result["技术"])
        self.assertEqual([], result["知识"])
        self.assertEqual([], result["动机"])

    def test_hiring_conditions_are_not_written_as_abilities(self) -> None:
        source = "本科以上学历，三年工作经验，掌握软件工程专业知识和 Python。"
        result = validate_result(
            {
                "知识": ["本科以上学历", "软件工程专业知识"],
                "技术": ["三年工作经验", "Python"],
            },
            source,
        )
        self.assertEqual(["软件工程专业知识"], result["知识"])
        self.assertEqual(["Python"], result["技术"])

    def test_written_json_is_compatible_with_existing_extractor(self) -> None:
        analysis = json.dumps(
            {
                "知识": ["大语言模型"],
                "技术": ["Python"],
                "动机": ["主动学习"],
                "特质": ["团队协作"],
                "自我概念": ["责任心"],
            },
            ensure_ascii=False,
        )
        document = JobDocument(
            jd_id="jd:1",
            source_file="test.csv",
            source_category="",
            raw_job_id="job:1",
            company_id="company:1",
            company_name="测试公司",
            title="AI工程师",
            canonical_role="AI工程师",
            description="熟悉大语言模型和 Python，主动学习，重视团队协作，具有责任心。",
            tags="",
            ability_analysis=analysis,
            industry="",
            education="",
            experience="",
            salary="",
            location="",
            posted_at=None,
            level="",
            exact_hash="",
            simhash=0,
        )
        registry = SkillRegistry(
            Path(__file__).resolve().parents[1]
            / "trusted_graph_agent"
            / "skills_registry.json"
        )
        candidates = AbilityAnalysisExtractor(registry).extract(document)
        self.assertIn(("Python", "技术"), {(item.skill_name, item.competency_category) for item in candidates})

    def test_existing_result_skips_model_call(self) -> None:
        class NeverCalledClient:
            model = "test-model"

            def extract(self, title: str, description: str, tags: str):
                raise AssertionError("model must not be called")

        with tempfile.TemporaryDirectory() as temporary:
            cache = JsonlCache(Path(temporary) / "cache.jsonl")
            index, row, state = process_record(
                0,
                {
                    "职位名称": "Python开发工程师",
                    "职位描述": "负责 Python 开发",
                    "能力提取结果": '{"技术":["Python"]}',
                },
                NeverCalledClient(),  # type: ignore[arg-type]
                cache,
                overwrite=False,
            )
            self.assertEqual(0, index)
            self.assertTrue(state["skipped"])
            self.assertEqual('{"技术":["Python"]}', row["能力提取结果"])

    def test_existing_result_alias_is_copied_to_canonical_field(self) -> None:
        class NeverCalledClient:
            model = "test-model"

            def extract(self, title: str, description: str, tags: str):
                raise AssertionError("model must not be called")

        with tempfile.TemporaryDirectory() as temporary:
            _, row, state = process_record(
                0,
                {
                    "title": "Python开发工程师",
                    "description": "负责 Python 开发",
                    "ability_analysis": '{"技术":["Python"]}',
                },
                NeverCalledClient(),  # type: ignore[arg-type]
                JsonlCache(Path(temporary) / "cache.jsonl"),
                overwrite=False,
            )
        self.assertTrue(state["skipped"])
        self.assertEqual('{"技术":["Python"]}', row["能力提取结果"])


if __name__ == "__main__":
    unittest.main()
