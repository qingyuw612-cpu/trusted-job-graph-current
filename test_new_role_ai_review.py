from __future__ import annotations

import pytest

from new_role_discovery.qwen_reviewer import (
    SemanticReviewer,
    SemanticReviewResult,
)
from new_role_discovery.responsibility_summary import (
    summarize_responsibility_evidence,
)


def test_responsibility_fallback_removes_recruiting_noise_and_binds_evidence() -> None:
    result = summarize_responsibility_evidence(
        [
            {"jd_id": "jd-noise", "text": "注：base广州、杭州、长沙均可"},
            {
                "jd_id": "jd-duty",
                "text": "负责ADAS系统功能需求和技术规范定义，整理编制相关需求文档。",
            },
        ]
    )

    assert len(result) == 1
    assert result[0]["evidence_jd_ids"] == ["jd-duty"]
    assert "base" not in result[0]["text"].lower()
    assert result[0]["text"] != "负责ADAS系统功能需求和技术规范定义，整理编制相关需求文档。"


def test_definition_fallback_publishes_condensed_duties_for_future_runs() -> None:
    reviewer = object.__new__(SemanticReviewer)
    definition, error = reviewer._assemble_role_definition(
        {
            "candidate_title": "行车系统工程师",
            "responsibility_evidence": [
                {
                    "jd_id": "jd-1",
                    "text": "负责ADAS系统功能需求和技术规范定义，整理编制相关需求文档。",
                }
            ],
            "required_skill_draft": [
                {"skill": "需求分析", "evidence": [{"jd_id": "jd-1"}]},
            ],
        },
        {"canonical_name": "行车系统工程师", "reason": "职责边界待复核"},
    )

    assert error == ""
    assert definition["core_responsibilities"]
    assert definition["core_responsibilities"][0]["evidence_jd_ids"] == ["jd-1"]


def test_role_review_normalizes_legacy_decision_and_binds_packet_evidence() -> None:
    packet = {
        "candidate_title": "AI应用工程师",
        "nearest_roles": [{"role": "算法工程师"}],
        "responsibility_evidence": [
            {"jd_id": "jd-1", "text": "开发企业级大模型应用"},
            {"jd_id": "jd-2", "text": "建设智能体工作流"},
        ],
    }

    normalized = SemanticReviewer._normalize_role_classification(
        {
            "decision": "NEW_ROLE_CANDIDATE",
            "canonical_name": "AI应用工程师",
            "reason": "形成了区别于通用算法研发的应用交付职责边界",
            "confidence": "0.88",
        },
        packet,
    )

    assert normalized == {
        "semantic_class": "NEW_ROLE",
        "canonical_name": "AI应用工程师",
        "nearest_existing_role": "算法工程师",
        "reason": "形成了区别于通用算法研发的应用交付职责边界",
        "evidence_jd_ids": ["jd-1", "jd-2"],
        "confidence": 0.88,
        "confidence_source": "MODEL_REPORTED",
        "recommended_action": "提交人工审批新岗位",
    }


def test_role_review_extracts_json_object_from_provider_wrapper() -> None:
    packet = {
        "candidate_title": "数据科学家",
        "nearest_roles": [{"role": "机器学习工程师"}],
        "responsibility_evidence": [{"jd_id": "jd-3", "text": "开展统计建模"}],
    }
    raw = (
        "审核结果如下：\n"
        '{"semantic_class":"SPECIALIZATION","canonical_name":"数据科学家",'
        '"nearest_existing_role":"机器学习工程师","reason":"更侧重统计分析与业务洞察",'
        '"evidence_jd_ids":["jd-3"],"confidence":0.82,'
        '"recommended_action":"作为岗位细分提交人工审批"}'
    )

    reviewer = object.__new__(SemanticReviewer)
    parsed = reviewer._parse_and_validate("role_classification", raw, packet)

    assert parsed["semantic_class"] == "SPECIALIZATION"
    assert parsed["canonical_name"] == "数据科学家"
    assert parsed["evidence_jd_ids"] == ["jd-3"]


def test_role_review_accepts_compact_line_contract() -> None:
    packet = {
        "candidate_title": "AI产品经理",
        "nearest_roles": [{"role": "产品经理"}],
        "responsibility_evidence": [{"jd_id": "jd-4", "text": "规划大模型产品"}],
    }
    reviewer = object.__new__(SemanticReviewer)

    parsed = reviewer._parse_and_validate(
        "role_classification",
        "SPECIALIZATION｜AI产品经理｜0.91｜负责AI产品规划，属于产品经理的稳定细分方向",
        packet,
    )

    assert parsed["semantic_class"] == "SPECIALIZATION"
    assert parsed["canonical_name"] == "AI产品经理"
    assert parsed["confidence"] == 0.91


def test_role_definition_accepts_compact_lite_line_and_binds_evidence() -> None:
    packet = {
        "candidate_title": "AI应用工程师",
        "responsibility_evidence": [
            {"jd_id": "jd-1", "text": "开发企业级大模型应用"},
            {"jd_id": "jd-2", "text": "建设智能体工作流"},
        ],
    }
    reviewer = object.__new__(SemanticReviewer)

    parsed = reviewer._parse_and_validate(
        "role_definition",
        "负责大模型应用的工程交付｜将大模型能力集成为可交付应用；设计智能体任务编排流程｜Python；RAG｜Docker｜jd-1,jd-2",
        packet,
    )

    assert parsed["role_boundary"] == "负责大模型应用的工程交付"
    assert parsed["core_responsibilities"][1]["evidence_jd_ids"] == ["jd-1", "jd-2"]
    assert [item["skill"] for item in parsed["required_skills"]] == ["Python", "RAG"]


def test_role_definition_accepts_duty_only_summary_and_uses_packet_skills() -> None:
    packet = {
        "candidate_title": "AI应用工程师",
        "responsibility_evidence": [
            {"jd_id": "jd-1", "text": "开发企业级大模型应用"},
        ],
        "required_skill_draft": [
            {"skill": "Python", "evidence": [{"jd_id": "jd-1"}]},
        ],
    }
    reviewer = object.__new__(SemanticReviewer)

    parsed = reviewer._parse_and_validate(
        "role_definition",
        "大模型应用工程交付｜将模型能力集成为业务应用；建设应用评测与迭代流程｜jd-1",
        packet,
    )

    assert len(parsed["core_responsibilities"]) == 2
    assert parsed["required_skills"][0]["skill"] == "Python"


def test_role_definition_accepts_lite_two_part_line_without_ids() -> None:
    packet = {
        "candidate_title": "大数据调度工具开发工程师",
        "responsibility_evidence": [
            {"jd_id": "jd-1", "text": "设计分布式任务调度引擎"},
        ],
        "required_skill_draft": [
            {"skill": "Java", "evidence": [{"jd_id": "jd-1"}]},
        ],
    }
    reviewer = object.__new__(SemanticReviewer)

    parsed = reviewer._parse_and_validate(
        "role_definition",
        "大数据调度工具开发工程师｜建设可扩展的分布式任务调度能力；必备技能Java；加分技能无。",
        packet,
    )

    assert parsed["role_boundary"] == "建设可扩展的分布式任务调度能力"
    assert parsed["core_responsibilities"][0]["evidence_jd_ids"] == ["jd-1"]
    assert [item["skill"] for item in parsed["required_skills"]] == ["Java"]


def test_role_definition_accepts_prose_recovery_that_copies_raw_jd() -> None:
    packet = {
        "candidate_title": "传感器硬件工程师",
        "responsibility_evidence": [
            {"jd_id": "jd-2", "text": "负责传感器硬件设计与验证"},
        ],
        "required_skill_draft": [
            {"skill": "电路设计", "evidence": [{"jd_id": "jd-2"}]},
        ],
    }
    reviewer = object.__new__(SemanticReviewer)

    parsed = reviewer._parse_and_validate(
        "role_definition",
        "围绕传感器硬件的设计、调试和量产验证开展工程工作。",
        packet,
    )
    assert parsed["role_boundary"]
    assert parsed["core_responsibilities"]


def test_role_review_uses_lite_for_evidence_bound_definition() -> None:
    packet = {
        "candidate_title": "AI应用工程师",
        "nearest_roles": [{"role": "算法工程师"}],
        "statistics": {"confirmation_state": "MULTI_WINDOW_CONFIRMED"},
        "responsibility_evidence": [
            {"jd_id": "jd-1", "text": "开发企业级大模型应用"},
        ],
        "candidate_skills": [
            {
                "skill": "RAG",
                "evidence": [{"jd_id": "jd-1", "text": "建设RAG应用"}],
            },
        ],
    }
    calls: list[str] = []
    reviewer = object.__new__(SemanticReviewer)

    def fake_review(kind: str, evidence_packet: dict) -> SemanticReviewResult:
        calls.append(kind)
        if kind == "role_classification":
            return SemanticReviewResult(
                status="COMPLETED",
                source="LLM",
                analysis={
                    "semantic_class": "NEW_ROLE",
                    "canonical_name": "AI应用工程师",
                    "nearest_existing_role": "算法工程师",
                    "reason": "形成独立的应用交付职责边界",
                    "evidence_jd_ids": ["jd-1"],
                    "confidence": 0.91,
                    "recommended_action": "提交人工审批新岗位",
                },
            )
        assert evidence_packet["confirmed_classification"]["canonical_name"] == "AI应用工程师"
        return SemanticReviewResult(
            status="COMPLETED",
            source="LLM",
            analysis={
                "role_boundary": "负责大模型应用从需求到交付的完整工程链路。",
                "core_responsibilities": [
                    {"text": "开发企业级大模型应用", "evidence_jd_ids": ["jd-1"]},
                ],
                "required_skills": [
                    {"skill": "RAG", "evidence_jd_ids": ["jd-1"]},
                ],
                "bonus_skills": [],
                "industry_scenarios": [],
                "risks": [],
            },
        )

    reviewer._review = fake_review
    result = reviewer.review_role(packet)

    assert calls == ["role_classification", "role_definition"]
    assert result.status == "COMPLETED"
    assert "LLM_DEFINITION" in result.source
    assert result.analysis["role_boundary"].startswith("负责大模型应用")
    assert result.analysis["core_responsibilities"][0]["evidence_jd_ids"] == ["jd-1"]


def test_role_review_still_generates_neutral_definition_when_classification_fails() -> None:
    packet = {
        "candidate_title": "传感器硬件工程师",
        "nearest_roles": [{"role": "硬件工程师"}],
        "statistics": {"confirmation_state": "SINGLE_WINDOW_PROVISIONAL"},
        "responsibility_evidence": [
            {"jd_id": "jd-2", "text": "负责传感器硬件设计与验证"},
        ],
    }
    calls: list[str] = []
    reviewer = object.__new__(SemanticReviewer)

    def fake_review(kind: str, _: dict) -> SemanticReviewResult:
        calls.append(kind)
        if kind == "role_classification":
            return SemanticReviewResult(
                status="FAILED",
                source="LLM",
                error="TimeoutError",
            )
        return SemanticReviewResult(
            status="COMPLETED",
            source="LLM",
            analysis={
                "role_boundary": "围绕传感器硬件设计、调试和验证开展工作。",
                "core_responsibilities": [
                    {"text": "负责传感器硬件设计与验证", "evidence_jd_ids": ["jd-2"]},
                ],
                "required_skills": [],
                "bonus_skills": [],
                "industry_scenarios": [],
                "risks": ["岗位边界仍需更多招聘证据确认"],
            },
        )

    reviewer._review = fake_review
    result = reviewer.review_role(packet)

    assert calls == ["role_classification", "role_definition"]
    assert result.status == "PARTIAL"
    assert result.analysis["semantic_class"] == "UNCERTAIN"
    assert result.analysis["canonical_name"] == "传感器硬件工程师"
    assert "LLM_DEFINITION" in result.source
    assert result.analysis["role_boundary"].startswith("围绕传感器硬件")


def test_definition_fallback_is_explicit_and_never_empty() -> None:
    reviewer = object.__new__(SemanticReviewer)
    definition, error = reviewer._assemble_role_definition(
        {
            "candidate_title": "企业知识库工程师",
            "responsibility_evidence": [],
            "required_skill_draft": [],
            "candidate_skills": [],
        },
        {"canonical_name": "企业知识库工程师", "reason": "待人工确认"},
    )

    # The caller keeps the mechanical candidate even when the source lacks a
    # usable responsibility sentence; the explicit error is auditable.
    assert definition["core_responsibilities"] == []
    assert isinstance(error, str)


def test_definition_fallback_promotes_strongest_evidence_backed_skill() -> None:
    reviewer = object.__new__(SemanticReviewer)
    definition, error = reviewer._assemble_role_definition(
        {
            "candidate_title": "企业知识库工程师",
            "responsibility_evidence": [
                {"jd_id": "jd-1", "text": "负责企业知识库检索链路建设"},
            ],
            "required_skill_draft": [],
            "bonus_skill_draft": [],
            "candidate_skills": [
                {
                    "skill": "RAG",
                    "company_count": 1,
                    "company_coverage": 0.2,
                    "evidence": [{"jd_id": "jd-1", "text": "熟悉RAG"}],
                },
                {
                    "skill": "向量数据库",
                    "company_count": 1,
                    "company_coverage": 0.2,
                    "evidence": [{"jd_id": "jd-2", "text": "使用向量数据库"}],
                },
            ],
        },
        {
            "semantic_class": "UNCERTAIN",
            "canonical_name": "企业知识库工程师",
            "reason": "待人工确认",
            "evidence_jd_ids": ["jd-1"],
            "confidence": 0.0,
            "recommended_action": "保留观察",
        },
    )

    assert [item["skill"] for item in definition["required_skills"]] == [
        "RAG",
        "向量数据库",
    ]
    assert error == ""


def test_role_level_suffix_is_forced_to_existing_role_specialization() -> None:
    packet = {
        "candidate_title": "硬件测试leader",
        "nearest_roles": [
            {
                "role": "硬件测试工程师",
                "title_similarity": 0.47,
            }
        ],
        "statistics": {"confirmation_state": "RECENT_NEW_TITLE_CLUSTER_CANDIDATE"},
        "responsibility_evidence": [
            {"jd_id": "jd-3", "text": "搭建硬件测试体系并制定测试方案"},
        ],
    }
    reviewer = object.__new__(SemanticReviewer)

    def fake_review(kind: str, _: dict) -> SemanticReviewResult:
        if kind == "role_classification":
            return SemanticReviewResult(
                status="COMPLETED",
                source="LLM",
                analysis={
                    "semantic_class": "UNCERTAIN",
                    "canonical_name": "",
                    "nearest_existing_role": "硬件测试工程师",
                    "reason": "边界不足",
                    "evidence_jd_ids": ["jd-3"],
                    "confidence": 0.2,
                    "recommended_action": "继续观察",
                },
            )
        return SemanticReviewResult(
            status="COMPLETED",
            source="LLM",
            analysis={
                "role_boundary": "硬件测试工程师",
                "core_responsibilities": [
                    {"text": "搭建硬件测试体系", "evidence_jd_ids": ["jd-3"]},
                ],
                "required_skills": [],
                "bonus_skills": [],
                "industry_scenarios": [],
                "risks": [],
            },
        )

    reviewer._review = fake_review
    result = reviewer.review_role(packet)

    assert result.analysis["semantic_class"] == "SPECIALIZATION"
    assert result.analysis["canonical_name"] == "硬件测试工程师"
    assert result.analysis["recommended_action"] == "作为既有岗位职级细分保留，不按全新岗位处理"
    assert "POLICY_GUARD" in result.source


def test_specialization_keeps_meaningful_ai_agent_qualifier() -> None:
    packet = {
        "candidate_title": "AIagent产品经理",
        "title_variants": [
            {"title": "AI Agent 产品经理", "count": 3},
            {"title": "AI Agent 产品经理（微信生态方向）", "count": 1},
        ],
        "nearest_roles": [{"role": "产品经理", "title_similarity": 0.53}],
        "statistics": {"confirmation_state": "RECENT_NEW_TITLE_CLUSTER_CANDIDATE"},
        "responsibility_evidence": [
            {"jd_id": "jd-4", "text": "负责智能体产品规划与落地"},
        ],
    }
    reviewer = object.__new__(SemanticReviewer)

    def fake_review(kind: str, _: dict) -> SemanticReviewResult:
        if kind == "role_classification":
            return SemanticReviewResult(
                status="COMPLETED",
                source="LLM",
                analysis={
                    "semantic_class": "UNCERTAIN",
                    "canonical_name": "",
                    "nearest_existing_role": "产品经理",
                    "reason": "属于产品经理的智能体方向",
                    "evidence_jd_ids": ["jd-4"],
                    "confidence": 0.2,
                    "recommended_action": "继续观察",
                },
            )
        return SemanticReviewResult(
            status="COMPLETED",
            source="LLM",
            analysis={
                "role_boundary": "负责智能体产品规划与落地",
                "core_responsibilities": [
                    {"text": "负责智能体产品规划与落地", "evidence_jd_ids": ["jd-4"]},
                ],
                "required_skills": [],
                "bonus_skills": [],
                "industry_scenarios": [],
                "risks": [],
            },
        )

    reviewer._review = fake_review
    result = reviewer.review_role(packet)

    assert result.analysis["semantic_class"] == "SPECIALIZATION"
    assert result.analysis["canonical_name"] == "AI Agent 产品经理"
    assert "POLICY_GUARD" in result.source
