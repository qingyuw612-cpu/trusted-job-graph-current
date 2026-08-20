"""匹配逻辑测试 — 纯函数，零外部依赖。"""
import pytest

from src.core.matching import _item_match, _normalize, dim_coverage, match_skills_in_text


class TestNormalize:
    def test_strip_punct_and_lower(self):
        assert _normalize(" 模型、AIGC、RAG 知识 ") == "模型aigcrag知识"

    def test_fullwidth_to_halfwidth(self):
        assert _normalize("（IC设计）") == "ic设计"

    def test_empty_input(self):
        assert _normalize("") == ""
        assert _normalize(None) == ""


class TestItemMatch:
    def test_exact_equal(self):
        assert _item_match("半导体工艺知识", "半导体工艺知识")

    def test_contains_condensed(self):
        assert _item_match("模拟IC设计经验", "5年以上模拟IC设计经验")

    def test_window_major(self):
        assert _item_match("人工智能/计算机、信息管理专业", "计算机相关专业")

    def test_window_model(self):
        assert _item_match(
            "大模型、AIGC、RAG、智能客服等AI产品形态知识",
            "大模型API与Prompt工程认知",
        )

    def test_en_token(self):
        assert _item_match(
            "大模型应用设计（智能客服、Agent工作流）",
            "Agent框架（LangChain/LangGraph）认知",
        )

    def test_degree_satisfied(self):
        assert _item_match("本科/硕士学历", "本科及以上学历")

    def test_degree_not_enough(self):
        assert not _item_match("本科/硕士学历", "博士学历")

    def test_degree_overqualified(self):
        assert _item_match("博士学历", "本科及以上学历")

    def test_overlap_fallback(self):
        assert _item_match("沟通协调能力", "沟通能力好")

    def test_unrelated_rejected(self):
        assert not _item_match("原型设计（Axure、Figma）", "精通op-amp/LDO等模拟模块设计")

    def test_empty_rejected(self):
        assert not _item_match("", "半导体工艺知识")
        assert not _item_match("半导体工艺知识", "")


class TestDimCoverage:
    def test_no_jd_means_no_gap(self):
        assert dim_coverage([], []) == 1.0

    def test_no_jd_with_candidate(self):
        assert dim_coverage(["主动性"], []) == 1.0

    def test_no_candidate_means_zero(self):
        assert dim_coverage([], ["学习能力强"]) == 0.0

    def test_partial_coverage(self):
        assert dim_coverage(["本科/硕士学历"], ["本科及以上学历", "博士学历"]) == 0.5

    def test_full_coverage(self):
        assert dim_coverage(["C/C++编程", "Linux"], ["C/C++编程", "Linux开发"]) == 1.0


class TestMatchSkillsInText:
    @staticmethod
    def _skills(*names):
        return [
            {"name": n, "category": "技术", "weight": 1.0, "rank": i + 1}
            for i, n in enumerate(names)
        ]

    def test_hit_miss_counts(self):
        skills = self._skills("Python", "C++", "PyTorch")
        result = match_skills_in_text("熟悉 Python 与 PyTorch 开发", skills)
        assert result["hit_count"] == 2
        assert result["total"] == 3
        assert {s["name"] for s in result["hit"]} == {"Python", "PyTorch"}
        assert {s["name"] for s in result["miss"]} == {"C++"}
        assert result["by_dim"]["skill"]["hit_count"] == 2

    def test_normalized_matching_with_positions(self):
        skills = self._skills("学习能力强")
        result = match_skills_in_text("本人学习能力强，能快速上手新框架", skills)
        assert result["hit_count"] == 1
        assert result["hit"][0]["positions"]  # 高亮位置非空

    def test_short_token_not_false_positive(self):
        # "C++" 归一化后为 "c"（<2 字符）→ 不命中；LoRA 无对应文本 → 不命中
        skills = self._skills("C++", "LoRA")
        result = match_skills_in_text("熟悉 Python 编程", skills)
        assert result["hit_count"] == 0

    def test_by_dim_structure(self):
        skills = [
            {"name": "深度学习", "category": "知识", "weight": 1.0, "rank": 1},
            {"name": "PyTorch", "category": "技术", "weight": 1.0, "rank": 2},
        ]
        result = match_skills_in_text("深度学习与 PyTorch", skills)
        assert set(result["by_dim"]) == {"knowledge", "skill"}
        assert result["by_dim"]["knowledge"]["hit_count"] == 1
        assert result["by_dim"]["knowledge"]["total"] == 1
        assert result["by_dim"]["skill"]["hit_count"] == 1
        assert result["by_dim"]["skill"]["total"] == 1

    def test_unknown_category_skipped(self):
        skills = [{"name": "未知技能", "category": "未知类别", "weight": 1.0, "rank": 1}]
        result = match_skills_in_text("未知技能", skills)
        assert result["hit_count"] == 0
        assert result["total"] == 1
        assert result["by_dim"] == {}

    def test_degree_requirement_semantic_hit(self):
        skills = [{"name": "本科及以上学历", "category": "任职条件", "weight": 1.0, "rank": 1}]
        result = match_skills_in_text("某某大学（本科）计算机专业", skills)
        assert result["hit_count"] == 1

    def test_degree_requirement_miss_when_underqualified(self):
        skills = [{"name": "硕士及以上学历", "category": "任职条件", "weight": 1.0, "rank": 1}]
        result = match_skills_in_text("某某大学（本科）计算机专业", skills)
        assert result["hit_count"] == 0
        assert result["miss"][0]["name"] == "硕士及以上学历"

    def test_degree_requirement_overqualified_hit(self):
        skills = [{"name": "本科及以上学历", "category": "任职条件", "weight": 1.0, "rank": 1}]
        result = match_skills_in_text("某某大学（硕士）计算机专业", skills)
        assert result["hit_count"] == 1

    def test_degree_requirement_miss_without_degree(self):
        skills = [{"name": "本科及以上学历", "category": "任职条件", "weight": 1.0, "rank": 1}]
        result = match_skills_in_text("熟悉 Python 与 PyTorch", skills)
        assert result["hit_count"] == 0

    def test_non_degree_skill_keeps_substring_matching(self):
        skills = self._skills("本科相关课程")
        result = match_skills_in_text("修读过本科相关课程", skills)
        assert result["hit_count"] == 1

