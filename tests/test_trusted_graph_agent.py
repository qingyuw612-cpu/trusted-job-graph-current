from __future__ import annotations

import csv
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from trusted_graph_agent.api_server import GraphRepository
from trusted_graph_agent.config import AgentConfig
from trusted_graph_agent.extractors import AbilityAnalysisExtractor, EvidenceVerifier
from trusted_graph_agent.models import JobDocument, SkillCandidate
from trusted_graph_agent.normalization_experiment import corrected_category, lexical_similarity, merge_allowed, normalize_surface
from trusted_graph_agent.pipeline import TrustedGraphAgent
from trusted_graph_agent.registry import SkillRegistry
from trusted_graph_agent.taxonomy import RoleTaxonomy
from trusted_graph_agent.text_utils import simhash64, text_hash, time_decay


ROOT = Path(__file__).resolve().parents[1]


def make_document(jd_id: str, company: str, description: str) -> JobDocument:
    return JobDocument(
        jd_id=jd_id,
        source_file="fixture.csv",
        source_category="人工智能",
        raw_job_id=jd_id,
        company_id=company,
        company_name=f"公司{company}",
        title="Python开发工程师",
        canonical_role="Python开发工程师",
        description=description,
        tags="",
        ability_analysis="",
        industry="计算机软件",
        education="本科",
        experience="3年",
        salary="",
        location="深圳",
        posted_at=datetime(2025, 7, 1),
        level="中级",
        exact_hash=text_hash(company, "Python开发工程师", description),
        simhash=simhash64(description),
    )


class RegistryAndEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = SkillRegistry(ROOT / "trusted_graph_agent" / "skills_registry.json")

    def test_alias_normalization_and_ascii_boundaries(self) -> None:
        self.assertEqual(self.registry.resolve("PyTest").canonical_name, "Pytest")
        java = self.registry.resolve("Java")
        self.assertIsNone(self.registry.find_alias("熟悉 JavaScript 前端开发", java.aliases))

    def test_evidence_verifier_blocks_hallucination(self) -> None:
        document = make_document("jd:1", "company:1", "负责 Python 服务开发，熟悉 SQL。")
        candidates = [
            SkillCandidate("Python", "Python", "required", "负责 Python 服务开发", 0.95, "LLM_WEBHOOK"),
            SkillCandidate("PyTorch", "PyTorch", "required", "熟悉 PyTorch", 0.90, "LLM_WEBHOOK"),
        ]
        result = EvidenceVerifier(self.registry).verify(document, candidates)
        statuses = {item.skill_name: item.evidence_status for item in result.evidences}
        self.assertEqual(statuses["Python"], "VERIFIED")
        self.assertEqual(statuses["PyTorch"], "REJECTED_HALLUCINATION")
        self.assertEqual(len(result.reviews), 1)

    def test_existing_ability_analysis_is_parsed_by_five_categories(self) -> None:
        document = make_document("jd:analysis", "company:1", "负责智能体工作流编排，使用 Python 开发。")
        document.ability_analysis = (
            "1. 知识：大语言模型；智能体原理\n"
            "2. 技术：Python；智能体工作流编排\n"
            "3. 动机：结果导向\n"
            "4. 特质：沟通能力\n"
            "5. 自我概念：责任心"
        )
        candidates = AbilityAnalysisExtractor(self.registry).extract(document)
        self.assertGreaterEqual(len(candidates), 7)
        categories = {item.skill_name: item.competency_category for item in candidates}
        self.assertEqual(categories["Python"], "技术")
        self.assertEqual(categories["智能体工作流编排"], "技术")

        result = EvidenceVerifier(self.registry).verify(document, candidates)
        statuses = {item.skill_name: item.evidence_status for item in result.evidences}
        self.assertEqual(statuses["智能体工作流编排"], "VERIFIED")
        self.assertEqual(statuses["智能体原理"], "ANALYSIS_ONLY")

    def test_json_ability_analysis_is_parsed_by_category(self) -> None:
        document = make_document("jd:json-analysis", "company:1", "使用 Python 和 Spring Boot 开发。")
        document.ability_analysis = (
            '{"知识":"软件工程知识","技术":"Python、Spring Boot",'
            '"动机":"主动学习","特质":"沟通能力","自我概念":"团队协作意识"}'
        )
        candidates = AbilityAnalysisExtractor(self.registry).extract(document)
        categories = {item.skill_name: item.competency_category for item in candidates}
        self.assertEqual(categories["Python"], "技术")
        self.assertEqual(categories["Spring Boot"], "技术")
        self.assertEqual(categories["主动学习"], "动机")

    def test_english_five_dimension_json_is_mapped_to_chinese_categories(self) -> None:
        document = make_document(
            "jd:english-analysis",
            "company:1",
            "使用 PHP、Python 和 Redis 开发，具备学习能力和团队协作意识。",
        )
        document.ability_analysis = json.dumps(
            {
                "Knowledge": ["软件工程知识"],
                "Skill": ["PHP", "Python", "Redis"],
                "Motivation": ["主动学习"],
                "Trait": ["学习能力"],
                "Self-concept": ["团队协作意识"],
            },
            ensure_ascii=False,
        )
        candidates = AbilityAnalysisExtractor(self.registry).extract(document)
        categories = {item.skill_name: item.competency_category for item in candidates}
        self.assertEqual(categories["软件工程知识"], "知识")
        self.assertEqual(categories["PHP"], "技术")
        self.assertEqual(categories["主动学习"], "动机")
        self.assertEqual(categories["学习能力"], "特质")
        self.assertEqual(categories["团队协作"], "自我概念")

    def test_mixed_language_dimension_aliases_are_merged_without_duplicates(self) -> None:
        document = make_document("jd:mixed-analysis", "company:1", "使用 Python 和 SQL 开发。")
        document.ability_analysis = json.dumps(
            {"技术": ["Python"], "skills": ["Python", "SQL"], "self_concept": []},
            ensure_ascii=False,
        )
        candidates = AbilityAnalysisExtractor(self.registry).extract(document)
        technical = [
            item.skill_name for item in candidates if item.competency_category == "技术"
        ]
        self.assertEqual(["Python", "SQL"], technical)


class PipelineLogicTests(unittest.TestCase):
    def test_normalization_surface_and_merge_guard(self) -> None:
        self.assertEqual(normalize_surface("具备良好的沟通协调能力"), "沟通协调能力")
        self.assertEqual(normalize_surface("熟练掌握 Python 开发经验"), "Python")
        self.assertGreater(lexical_similarity("用户需求分析", "需求分析"), 0.6)
        self.assertTrue(merge_allowed("用户需求分析", "需求分析", 0.7, 0.35))
        self.assertFalse(merge_allowed("数据分析", "数据治理", 0.2, 0.35))
        self.assertFalse(merge_allowed("1年以上工作经验", "3年以上工作经验", 0.8, 0.35))
        self.assertEqual(corrected_category("计算机相关专业本科", "知识"), "任职条件")
        self.assertEqual(corrected_category("电气工程专业", "知识"), "任职条件")
        self.assertEqual(corrected_category("原文未明确提及", "动机"), "噪声")
        self.assertEqual(corrected_category("无明确表述", "动机"), "噪声")
        self.assertEqual(corrected_category("没有对应描述", "特质"), "噪声")
        self.assertEqual(corrected_category("无", "自我概念"), "噪声")

    def test_model_meta_commentary_is_removed_from_skills(self) -> None:
        noise_phrases = [
            "特质和自我概念维度的直接描述要素",
            "动机和自我概念维度未出现直接对应的能力要素描述",
            "所有输出要素均严格对应原文表述",
            "原文中未出现动机维度的明确描述",
            "该内容未纳入胜任力模型分析",
        ]
        for phrase in noise_phrases:
            with self.subTest(phrase=phrase):
                self.assertEqual(corrected_category(phrase, "特质"), "噪声")

        self.assertEqual(corrected_category("多维数据分析", "技术"), "技术")

    def test_it_taxonomy_has_unique_roles_and_product_hierarchy(self) -> None:
        taxonomy = RoleTaxonomy(ROOT / "trusted_graph_agent" / "it_role_taxonomy.json")
        role_names = [row["role_name"] for row in taxonomy.roles]
        self.assertEqual(len(role_names), 55)
        self.assertEqual(len(role_names), len(set(role_names)))
        self.assertEqual(taxonomy.role("平台产品经理")["parent_role"], "产品经理")
        self.assertEqual(taxonomy.role("数据产品经理")["parent_role"], "产品经理")

    def test_twelve_month_half_life(self) -> None:
        reference = datetime(2025, 1, 1)
        value = datetime(2024, 1, 1)
        self.assertAlmostEqual(time_decay(value, reference, 12), 0.5, delta=0.01)

    def test_exact_dedup_and_cross_company_template_weight(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = AgentConfig(Path(temp), Path(temp) / "out")
            agent = TrustedGraphAgent(config)
            description = "负责 Python 后端服务开发，熟悉 Django、MySQL 和 Linux。"
            documents = [
                make_document("jd:1", "c1", description),
                make_document("jd:2", "c1", description),
                make_document("jd:3", "c2", description),
                make_document("jd:4", "c3", description),
            ]
            metrics = agent.deduplicate(documents)
            self.assertEqual(metrics["exact_duplicates"], 1)
            active = [item for item in documents if not item.is_duplicate]
            self.assertEqual(len(active), 3)
            self.assertTrue(all(item.template_cluster_id for item in active))
            self.assertTrue(all(item.template_weight < 1 for item in active))

    def test_full_extracted_entry_exact_dedup_and_near_duplicate_downweight(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = AgentConfig(Path(temp), Path(temp) / "out")
            agent = TrustedGraphAgent(config)
            extracted = (
                "知识：Java基础；技能：Spring Boot、MySQL、Redis；特质：团队协作、沟通能力；"
                "动机：持续学习和技术成长；自我概念：责任心强。"
            )
            exact_documents = [
                make_document("jd:1", "c1", "负责订单服务开发。"),
                make_document("jd:2", "c2", "负责支付服务开发。"),
            ]
            for document in exact_documents:
                document.ability_analysis = extracted
            metrics = agent.deduplicate(exact_documents)
            self.assertEqual(metrics["exact_extracted_entry_duplicates"], 1)
            self.assertEqual(exact_documents[1].duplicate_reason, "EXACT_EXTRACTED_ENTRY_DUPLICATE")

            near_documents = [
                make_document("jd:3", "c3", "负责会员服务开发。"),
                make_document("jd:4", "c4", "负责营销服务开发。"),
            ]
            near_documents[0].ability_analysis = extracted
            near_documents[1].ability_analysis = extracted.replace("责任心强", "责任心较强")
            metrics = agent.deduplicate(near_documents)
            self.assertEqual(metrics["exact_duplicates"], 0)
            self.assertEqual(metrics["template_clusters"], 1)
            self.assertTrue(all(not item.is_duplicate for item in near_documents))
            self.assertTrue(all(item.template_weight < 1 for item in near_documents))


class EndToEndTests(unittest.TestCase):
    def test_csv_to_database_neo4j_and_api(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "后端开发_output_folder"
            source.mkdir()
            csv_path = source / "python开发工程师.csv"
            fields = [
                "jobID", "companyID", "职位名称", "职位描述", "职位标签", "公司全称",
                "行业类型", "学历要求", "经验要求", "薪水", "工作地区", "时间", "能力分析结果",
            ]
            rows = [
                {
                    "jobID": str(index), "companyID": f"c{index}", "职位名称": "Python开发工程师",
                    "职位描述": "负责 Python 后端服务开发；熟悉 Django、SQL、Linux；具备良好沟通协调能力。",
                    "职位标签": "Python,Django,SQL", "公司全称": f"示例公司{index}", "行业类型": "计算机软件",
                    "学历要求": "本科", "经验要求": "3年", "薪水": "15-25K", "工作地区": "深圳",
                    "时间": f"2025-0{4 + index}-01 10:00:00",
                    "能力分析结果": "知识：后端开发知识；技术：Python；PyTorch；智能体工作流编排；特质：沟通能力",
                }
                for index in range(1, 5)
            ]
            with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)

            output = root / "output"
            bundle = TrustedGraphAgent(
                AgentConfig(
                    input_dir=root,
                    output_dir=output,
                    max_rows_per_file=0,
                    include_patterns=["python开发工程师.csv"],
                )
            ).run()

            self.assertEqual(bundle.run["state"], "COMPLETED")
            self.assertTrue((output / "knowledge_graph.db").exists())
            self.assertTrue((output / "neo4j" / "import.cypher").exists())
            self.assertTrue((output / "validation_report.json").exists())
            self.assertTrue(any(row["canonical_name"] == "Python" for row in bundle.skills))
            python_id = next(row["skill_id"] for row in bundle.skills if row["canonical_name"] == "Python")
            self.assertTrue(any(row["skill_id"] == python_id for row in bundle.role_skill_edges))
            self.assertTrue(any(row["skill_id"] == python_id for row in bundle.role_skill_snapshots))
            self.assertTrue(any(row["evidence_status"] == "ANALYSIS_ONLY" for row in bundle.jd_skill_edges))
            self.assertTrue(any(row["canonical_name"] == "智能体工作流编排" for row in bundle.skills))

            repository = GraphRepository(output / "knowledge_graph.db")
            self.assertEqual(repository.health()["state"], "COMPLETED")
            panorama = repository.panorama(stack="后端")
            self.assertGreater(panorama["stats"]["edges"], 0)
            role_nodes = [node for node in panorama["nodes"] if node["type"] == "role"]
            self.assertEqual(len(role_nodes), 1)
            self.assertEqual(role_nodes[0]["id"], bundle.roles[0]["role_id"])
            self.assertGreater(len(repository.skill_evidence(python_id)["evidence"]), 0)
            self.assertGreater(len(repository.role_timeline(bundle.roles[0]["role_id"])["windows"]), 0)

            with sqlite3.connect(output / "knowledge_graph.db") as connection:
                connection.executescript(
                    """
                    CREATE TABLE normalized_skills (
                        concept_id TEXT PRIMARY KEY, canonical_name TEXT, category TEXT, concept_status TEXT
                    );
                    CREATE TABLE normalized_role_skills (
                        role_id TEXT, concept_id TEXT, final_score REAL, company_count INTEGER,
                        jd_count INTEGER, verified_jd_count INTEGER, rank INTEGER
                    );
                    CREATE TABLE normalized_role_skill_snapshots (
                        role_id TEXT, concept_id TEXT, time_window TEXT, window_start TEXT,
                        final_score REAL, company_count INTEGER, jd_count INTEGER,
                        verified_jd_count INTEGER, rank INTEGER, trend TEXT, delta REAL
                    );
                    """
                )
                role_id = bundle.roles[0]["role_id"]
                connection.executemany(
                    "INSERT INTO normalized_skills VALUES (?, ?, ?, ?)",
                    [("concept:a", "季度技能A", "技术", "STANDARD"), ("concept:b", "季度技能B", "技术", "STANDARD")],
                )
                connection.executemany(
                    "INSERT INTO normalized_role_skills VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(role_id, "concept:a", 0.8, 3, 3, 3, 1), (role_id, "concept:b", 0.7, 3, 3, 3, 2)],
                )
                connection.executemany(
                    "INSERT INTO normalized_role_skill_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (role_id, "concept:a", "2025Q2", "2025-04-01", 0.8, 3, 3, 3, 1, "stable", 0.0),
                        (role_id, "concept:b", "2025Q3", "2025-07-01", 0.9, 3, 3, 3, 1, "emerging", 0.9),
                    ],
                )
                connection.commit()
            connection.close()
            quarter_two = repository.panorama(role_id=role_id, time_window="2025Q2", min_support=0, role_limit=1)
            quarter_three = repository.panorama(role_id=role_id, time_window="2025Q3", min_support=0, role_limit=1)
            self.assertEqual([node["label"] for node in quarter_two["nodes"] if node["type"] == "skill"], ["季度技能A"])
            self.assertEqual([node["label"] for node in quarter_three["nodes"] if node["type"] == "skill"], ["季度技能B"])


if __name__ == "__main__":
    unittest.main()
