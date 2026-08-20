"""排名与覆盖率计算测试 — 纯函数，零外部依赖。"""
import pytest

from src.core.ranking import _apply_idf, compute_dimension_hits, rank_roles


def _role(name, jd_count, skills, weights=None):
    weights = weights or [1.0] * len(skills)
    return {
        "role_name": name,
        "family_name": "算法",
        "domain_name": "AI",
        "jd_count": jd_count,
        "skills": [
            {
                "name": s,
                "category": "技术",
                "weight": float(weights[i]),
                "rank": i + 1,
            }
            for i, s in enumerate(skills)
        ],
    }


class TestApplyIdf:
    def test_single_role_unchanged(self):
        roles = [_role("R1", 1, ["Python", "PyTorch"])]
        out = _apply_idf(roles)
        assert all(s["weight"] == 1.0 for s in out[0]["skills"])

    def test_shared_skill_penalized(self):
        roles = [
            _role("R1", 1, ["通用技能", "特有A"]),
            _role("R2", 1, ["通用技能", "特有B"]),
        ]
        out = _apply_idf(roles)
        weights = {s["name"]: s["weight"] for s in out[0]["skills"]}
        assert weights["通用技能"] < 1.0
        assert weights["特有A"] > weights["通用技能"]

    def test_input_not_mutated(self):
        roles = [
            _role("R1", 1, ["通用技能", "特有A"]),
            _role("R2", 1, ["通用技能", "特有B"]),
        ]
        _apply_idf(roles)
        assert all(s["weight"] == 1.0 for r in roles for s in r["skills"])


class TestRankRoles:
    def test_descending_and_range(self):
        roles = [
            _role("全命中", 2, ["Python", "PyTorch", "深度学习", "机器学习"]),
            _role("半命中", 2, ["Python", "C++", "Java", "Go"]),
        ]
        ranked = rank_roles("Python、PyTorch、深度学习、机器学习", roles)
        assert ranked[0]["role_name"] == "全命中"
        assert ranked[0]["score"] >= ranked[1]["score"]
        assert all(0 <= r["score"] <= 1 for r in ranked)

    def test_topk_truncation(self):
        roles = [
            _role("R1", 1, ["Python", "PyTorch", "C++"]),
            _role("R2", 1, ["Java", "Go", "Rust"]),
            _role("R3", 1, ["React", "Vue", "Node"]),
        ]
        assert len(rank_roles("Python PyTorch C++", roles, topk=2)) == 2

    def test_few_skills_penalty(self):
        # 1 个技能全命中 → 覆盖率 1.0，但少条目惩罚 1/10 → 0.1
        roles = [_role("少条目", 1, ["Python"])]
        ranked = rank_roles("熟悉 Python", roles)
        assert ranked[0]["score"] == pytest.approx(0.1)

    def test_skips_zero_jd(self):
        roles = [_role("无JD", 0, ["Python"]), _role("正常", 1, ["Python"])]
        ranked = rank_roles("Python", roles)
        assert [r["role_name"] for r in ranked] == ["正常"]

    def test_skips_no_skills(self):
        roles = [_role("无技能", 1, [])]
        assert rank_roles("Python", roles) == []


class TestWeightedScore:
    def test_score_uses_final_score_weights(self):
        # Java 0.9 + Spring 0.7 命中，Vue 0.4 未命中
        # weighted = 1.6 / 2.0 = 0.8；penalty = 3/10 = 0.3 → score = 0.24
        roles = [
            _role("加权岗", 2, ["Java", "Spring", "Vue"], weights=[0.9, 0.7, 0.4])
        ]
        ranked = rank_roles("熟悉 Java 与 Spring", roles)
        assert ranked[0]["hit_skills"] == 2
        assert ranked[0]["score"] == pytest.approx(0.24)

    def test_score_differs_from_plain_hit_rate(self):
        roles = [
            _role("加权岗", 2, ["Java", "Spring", "Vue"], weights=[0.9, 0.7, 0.4])
        ]
        ranked = rank_roles("熟悉 Java 与 Spring", roles)
        # 纯命中率 2/3 ≈ 0.6667，加权覆盖率 0.8，两者不同
        assert ranked[0]["score"] != pytest.approx((2 / 3) * 0.3)

    def test_zero_total_weight_falls_back_to_hit_rate(self):
        roles = [
            _role("零权重", 2, ["Python", "PyTorch", "C++"], weights=[0.0, 0.0, 0.0])
        ]
        ranked = rank_roles("Python 与 PyTorch", roles)
        assert ranked[0]["score"] == pytest.approx((2 / 3) * 0.3)

    def test_use_idf_flag_toggles_rare_weighting(self):
        roles = [
            _role("R1", 2, ["通用技能", "特有A"]),
            _role("R2", 2, ["通用技能", "特有B"]),
        ]
        # 默认关闭 IDF：命中"通用技能" → 覆盖率 1/2 × penalty(0.2) = 0.1
        off = rank_roles("通用技能", roles, use_idf=False)[0]["score"]
        # 开启 IDF：通用技能 df=2 → idf=0 → 权重为 0 → 加权覆盖率 0
        on = rank_roles("通用技能", roles, use_idf=True)[0]["score"]
        assert off == pytest.approx(0.1)
        assert on == pytest.approx(0.0)


class TestComputeDimensionHits:
    def test_structure_and_coverage(self):
        skills = [
            {"name": "深度学习", "category": "知识", "weight": 1.0, "rank": 1},
            {"name": "PyTorch", "category": "技术", "weight": 1.0, "rank": 2},
            {"name": "C++", "category": "技术", "weight": 1.0, "rank": 3},
        ]
        result = compute_dimension_hits("深度学习与PyTorch", skills)
        assert result["knowledge"]["hit_count"] == 1
        assert result["knowledge"]["coverage"] == 1.0
        assert result["skill"]["hit_count"] == 1
        assert result["skill"]["miss"] == ["C++"]
        assert result["skill"]["coverage"] == 0.5
        assert result["skill"]["total"] == 2

    def test_empty_skills(self):
        assert compute_dimension_hits("Python", []) == {}

