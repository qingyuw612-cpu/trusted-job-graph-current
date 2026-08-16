from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from processing_layer.domain_filter import (
    DEFAULT_POLICY,
    DomainDecision,
    HybridITDomainClassifier,
)
from processing_layer.backfill_it_roles import resolve_domain_role
from processing_layer.normalize_with_demo import (
    FETCH_ELIGIBLE_ABILITIES_QUERY,
    FETCH_PAGE_QUERY,
)
from processing_layer.processor import (
    FETCH_ALL_VERSIONS_FORCE_QUERY,
    FETCH_ALL_VERSIONS_QUERY,
    FETCH_CURRENT_FORCE_QUERY,
    FETCH_CURRENT_INGEST_FORCE_QUERY,
    FETCH_CURRENT_QUERY,
    WRITE_BATCH_QUERY,
    build_document,
    clean_candidates,
    export_llm_queue,
    is_meta_noise,
)
from trusted_graph_agent.models import SkillCandidate
from trusted_graph_agent.taxonomy import RoleTaxonomy
from trusted_graph_agent.normalization_experiment import (
    NormalizationConfig,
    NormalizationExperiment,
    SkillCandidateGroup,
    UnionFind,
    is_noise_phrase,
)


class ProcessingLayerTests(unittest.TestCase):
    @staticmethod
    def domain_classifier() -> HybridITDomainClassifier:
        return HybridITDomainClassifier(DEFAULT_POLICY, embedder=None)

    @staticmethod
    def role_resolver() -> RoleTaxonomy:
        return RoleTaxonomy(
            Path(__file__).resolve().parents[1]
            / "trusted_graph_agent"
            / "it_role_taxonomy.json"
        )

    def test_it_role_resolver_uses_actual_title_for_contaminated_source(self) -> None:
        resolver = self.role_resolver()
        self.assertEqual(
            "Java开发工程师",
            resolver.resolve_title("高级Java开发工程师", "审计经理")["role_name"],
        )

    def test_it_role_resolver_rejects_non_taxonomy_source_label(self) -> None:
        resolver = self.role_resolver()
        self.assertIsNone(resolver.resolve_title("财务审计经理", "审计经理"))

    def test_51job_search_category_is_not_used_as_canonical_role_fallback(self) -> None:
        resolver = self.role_resolver()
        self.assertIsNone(
            resolve_domain_role(
                resolver,
                "开发工程师",
                "Python开发工程师",
                "SEARCH_CATEGORY",
            )
        )
        self.assertEqual(
            "Java开发工程师",
            resolve_domain_role(
                resolver,
                "高级Java开发工程师",
                "Python开发工程师",
                "SEARCH_CATEGORY",
            )["role_name"],
        )

    def test_llm_queue_export_pages_until_all_rows_are_written(self) -> None:
        rows = [
            {"version_id": f"rawversion:{index:02d}", "title": f"岗位{index}"}
            for index in range(5)
        ]

        class FakeClient:
            def query(self, _statement, parameters):
                cursor = parameters["cursor"]
                return [
                    row for row in rows if row["version_id"] > cursor
                ][: parameters["batch_size"]]

        class FakeRepository:
            client = FakeClient()

        with TemporaryDirectory() as directory:
            output = Path(directory) / "queue.jsonl"
            written = export_llm_queue(
                FakeRepository(),
                output,
                limit=0,
                source_platform="前程无忧",
                domain_label="IT",
                page_size=2,
            )
            exported = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(written, 5)
        self.assertEqual([row["version_id"] for row in exported], [row["version_id"] for row in rows])

    def test_domain_filter_accepts_multi_evidence_it_jobs(self) -> None:
        classifier = self.domain_classifier()
        rows = [
            {
                "declared_role": "Java开发工程师",
                "title": "高级Java开发工程师",
                "industry": "计算机软件",
                "description": "负责Spring微服务、API接口和数据库开发。",
            },
            {
                "declared_role": "产品经理",
                "title": "医疗信息化产品经理",
                "industry": "医疗服务",
                "description": "负责医院HIS系统平台需求分析、PRD、产品原型和版本迭代。",
            },
            {
                "declared_role": "嵌入式软件开发工程师",
                "title": "嵌入式软件开发工程师",
                "industry": "电子技术/半导体/集成电路",
                "description": "负责MCU、通信协议、硬件调试和嵌入式软件开发。",
            },
        ]
        decisions = classifier.classify_batch(rows)
        self.assertEqual([decision.label for decision in decisions], ["IT", "IT", "IT"])
        self.assertTrue(all(decision.positive_groups for decision in decisions))

    def test_domain_filter_rejects_non_it_jobs_hidden_in_it_source_files(self) -> None:
        classifier = self.domain_classifier()
        rows = [
            {
                "declared_role": "产品经理",
                "title": "医药推广产品经理",
                "industry": "制药/生物工程",
                "description": "负责药品推广、临床医学会议和专家库建设。",
            },
            {
                "declared_role": "产品经理",
                "title": "生产经理",
                "industry": "食品/饮料",
                "description": "负责生产线管理、生产计划、食品生产和车间人员管理。",
            },
            {
                "declared_role": "软件测试工程师",
                "title": "QA质量工程师",
                "industry": "家具",
                "description": "负责家具质量、供应商验货和原材料质量控制。",
            },
            {
                "declared_role": "产品经理",
                "title": "金融产品销售经理",
                "industry": "保险",
                "description": "负责寿险业务、金融产品销售、渠道拓展和销售指标。",
            },
        ]
        decisions = classifier.classify_batch(rows)
        self.assertEqual(
            [decision.label for decision in decisions],
            ["NON_IT", "NON_IT", "NON_IT", "NON_IT"],
        )

    def test_domain_filter_keeps_weak_generic_jobs_out_of_published_graph(self) -> None:
        decision = self.domain_classifier().classify_batch(
            [
                {
                    "declared_role": "产品经理",
                    "title": "产品经理",
                    "industry": "",
                    "description": "负责市场调研、竞品分析和产品推广。",
                }
            ]
        )[0]
        self.assertEqual(decision.label, "UNCERTAIN")
        self.assertIn("raw.domain_label = 'IT'", FETCH_PAGE_QUERY)
        self.assertIn("domain_label:'IT'", FETCH_ELIGIBLE_ABILITIES_QUERY)

    def test_incremental_queries_do_not_sort_the_full_raw_layer(self) -> None:
        self.assertNotIn("ORDER BY", FETCH_CURRENT_QUERY)
        self.assertNotIn("ORDER BY", FETCH_ALL_VERSIONS_QUERY)
        self.assertIn("processing_version", FETCH_CURRENT_QUERY)
        self.assertIn("processing_version", FETCH_ALL_VERSIONS_QUERY)

    def test_force_queries_keep_index_cursor_pagination(self) -> None:
        self.assertIn("USING INDEX", FETCH_CURRENT_FORCE_QUERY)
        self.assertIn("USING INDEX", FETCH_ALL_VERSIONS_FORCE_QUERY)
        self.assertIn("ORDER BY raw.version_id", FETCH_CURRENT_FORCE_QUERY)
        self.assertIn("ORDER BY raw.version_id", FETCH_ALL_VERSIONS_FORCE_QUERY)
        self.assertIn(
            "RawJDVersion(last_ingest_run_id, version_id)",
            FETCH_CURRENT_INGEST_FORCE_QUERY,
        )
        self.assertIn("raw.last_ingest_run_id = $ingest_run_id", FETCH_CURRENT_INGEST_FORCE_QUERY)

    def test_write_query_collapses_old_relationships_before_replacement(self) -> None:
        self.assertIn("collect(oldAbility) AS oldAbilities", WRITE_BATCH_QUERY)
        self.assertNotIn("DELETE oldAbility\nWITH row, processed", WRITE_BATCH_QUERY)
        self.assertNotIn("ProcessingReview", WRITE_BATCH_QUERY)
        self.assertNotIn("MERGE (task:ProcessingReview", WRITE_BATCH_QUERY)

    def test_normalization_exports_current_versions_only(self) -> None:
        self.assertIn("[:CURRENT_VERSION]->(raw)", FETCH_PAGE_QUERY)

    def test_meta_explanation_is_removed(self) -> None:
        self.assertTrue(is_meta_noise("动机和自我概念维度未出现直接对应的能力要素描述"))
        self.assertTrue(is_meta_noise("所有输出要素均严格对应原文表述"))
        self.assertTrue(is_meta_noise("特质和自我概念维度的具体要求"))
        self.assertTrue(is_meta_noise("无明确对应要素"))
        self.assertTrue(is_meta_noise("所有能力要素均直接提取自招聘文本"))
        self.assertTrue(is_meta_noise("采用不超过10个字的短词组形式呈现"))
        self.assertFalse(is_meta_noise("跨团队沟通能力"))

    def test_normalization_noise_filter_removes_meta_placeholders(self) -> None:
        self.assertTrue(is_noise_phrase("特质和自我概念维度的具体要求"))
        self.assertTrue(is_noise_phrase("无明确对应要素"))
        self.assertTrue(is_noise_phrase("无明确技术要求"))
        self.assertTrue(is_noise_phrase("故在两个维度中重复出现"))
        self.assertTrue(is_noise_phrase("归类为特质维度"))
        self.assertFalse(is_noise_phrase("跨团队沟通能力"))

    def test_clean_candidates_deduplicates_whole_terms(self) -> None:
        candidates = [
            SkillCandidate("Java", "Java", "required", "熟悉 Java", 0.9, "ABILITY_ANALYSIS", "技能"),
            SkillCandidate("Java", "Java", "required", "掌握 Java", 0.8, "ABILITY_ANALYSIS", "技能"),
        ]
        cleaned, removed = clean_candidates(candidates)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(removed, 1)

    def test_build_document_maps_raw_properties(self) -> None:
        document = build_document(
            "rawjob:1",
            {
                "version_id": "rawversion:1",
                "title": "Java开发工程师",
                "description": "熟悉 Spring Boot",
                "company_name": "示例公司",
                "ability_analysis_raw": "技能：Spring Boot",
            },
        )
        self.assertEqual(document.jd_id, "rawversion:1")
        self.assertEqual(document.ability_analysis, "技能：Spring Boot")
        self.assertEqual(document.raw_job_id, "rawjob:1")

    def test_adaptive_thresholds_follow_role_size(self) -> None:
        with TemporaryDirectory() as directory:
            experiment = NormalizationExperiment(
                Path(directory) / "unused.db",
                Path(directory) / "output",
                NormalizationConfig(),
                None,
            )
            self.assertEqual(experiment._adaptive_thresholds(2688, 1405, final=False), (8, 5))
            self.assertEqual(experiment._adaptive_thresholds(2688, 1405, final=True), (15, 8))
            self.assertEqual(experiment._adaptive_thresholds(31328, 11348, final=False), (32, 12))
            self.assertEqual(experiment._adaptive_thresholds(31328, 11348, final=True), (157, 57))

    def test_small_roles_use_sample_ratios(self) -> None:
        with TemporaryDirectory() as directory:
            experiment = NormalizationExperiment(
                Path(directory) / "unused.db",
                Path(directory) / "output",
                NormalizationConfig(),
                None,
            )
            self.assertEqual(experiment._adaptive_thresholds(50, 40, final=False), (5, 4))
            self.assertEqual(experiment._adaptive_thresholds(50, 40, final=True), (10, 8))

    def test_semantic_merge_rejects_conflicting_technical_tokens(self) -> None:
        self.assertFalse(NormalizationExperiment._semantic_merge_allowed("Java开发", "Python开发"))
        self.assertTrue(NormalizationExperiment._semantic_merge_allowed("数据分析能力", "数据分析技能"))

    def test_meaningful_overlap_ignores_generic_suffixes(self) -> None:
        self.assertTrue(
            NormalizationExperiment._has_meaningful_lexical_overlap(
                "跨部门沟通能力",
                "沟通协调技能",
            )
        )
        self.assertFalse(
            NormalizationExperiment._has_meaningful_lexical_overlap(
                "生产计划能力",
                "销售计划能力",
            )
        )

    def test_union_find_respects_cluster_size_cap(self) -> None:
        union_find = UnionFind(4)
        self.assertTrue(union_find.union(0, 1, max_size=2))
        self.assertTrue(union_find.union(2, 3, max_size=2))
        self.assertFalse(union_find.union(0, 2, max_size=2))

    def test_exact_cross_category_groups_are_harmonized(self) -> None:
        with TemporaryDirectory() as directory:
            experiment = NormalizationExperiment(
                Path(directory) / "unused.db",
                Path(directory) / "output",
                NormalizationConfig(),
                None,
            )
            technical = SkillCandidateGroup(
                key="技术|模拟电路",
                skill_id="skill:1",
                name="模拟电路",
                category="技术",
                jd_ids={"jd:1"},
                company_ids={"company:1"},
                verified_jds={"jd:1"},
            )
            knowledge = SkillCandidateGroup(
                key="知识|模拟电路",
                skill_id="skill:2",
                name="模拟电路",
                category="知识",
                jd_ids={"jd:2", "jd:3"},
                company_ids={"company:2", "company:3"},
                verified_jds={"jd:2", "jd:3"},
            )
            result = experiment._harmonize_exact_cross_category_groups([technical, knowledge])
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].category, "知识")
            self.assertEqual(result[0].jd_ids, {"jd:1", "jd:2", "jd:3"})
            self.assertEqual(experiment.cross_category_exact_merges, 1)

    def test_top_skill_selection_hard_deduplicates_semantic_variants(self) -> None:
        with TemporaryDirectory() as directory:
            config = NormalizationConfig(top_k={"技术": 3, "知识": 0, "特质": 0, "动机": 0, "自我概念": 0})
            experiment = NormalizationExperiment(
                Path(directory) / "unused.db",
                Path(directory) / "output",
                config,
                None,
            )
            rows = [
                {
                    "role": "测试岗位",
                    "category": "技术",
                    "concept_root": 1,
                    "canonical_name": "熟练使用办公软件",
                    "final_score": 0.8,
                },
                {
                    "role": "测试岗位",
                    "category": "技术",
                    "concept_root": 2,
                    "canonical_name": "OFFICE办公软件",
                    "final_score": 0.7,
                },
                {
                    "role": "测试岗位",
                    "category": "技术",
                    "concept_root": 3,
                    "canonical_name": "数据分析",
                    "final_score": 0.6,
                },
            ]
            concepts = {
                1: {"vector": np.array([1.0, 0.0], dtype="float32")},
                2: {"vector": np.array([0.99, 0.01], dtype="float32")},
                3: {"vector": np.array([0.0, 1.0], dtype="float32")},
            }
            selected = experiment._select_top_skills(rows, concepts)
            self.assertEqual(
                [row["canonical_name"] for row in selected],
                ["熟练使用办公软件", "数据分析"],
            )

    def test_selection_lexical_core_keeps_distinct_objects(self) -> None:
        core = NormalizationExperiment._selection_lexical_core
        self.assertEqual(core("熟练使用办公软件"), core("OFFICE办公软件"))
        self.assertEqual(sorted(core("客户关系维护")), sorted(core("维护客户关系")))
        self.assertEqual(core("制定生产计划"), core("生产计划"))
        self.assertNotEqual(core("销售计划"), core("生产计划"))
        self.assertNotEqual(core("Java开发"), core("Python开发"))
        self.assertNotEqual(core("Java"), core("JavaScript"))
        self.assertNotEqual(core("MySQL"), core("SQL"))
        self.assertNotEqual(core("质量管理"), core("质量意识"))


if __name__ == "__main__":
    unittest.main()
