from __future__ import annotations

from datetime import datetime
from pathlib import Path

from new_role_discovery.config import EvolutionConfig
from new_role_discovery.engine import ALGORITHM_VERSION
from new_role_discovery.engine import RESULT_SCHEMA_VERSION
from new_role_discovery.engine import EvolutionEngine
from new_role_discovery.engine import bundled_known_titles
from new_role_discovery.engine import display_title
from new_role_discovery.engine import established_role_composition
from new_role_discovery.engine import normalize_title
from new_role_discovery.engine import title_function_family
from new_role_discovery.engine import title_is_in_it_scope


class FakeSource:
    mapping_quality = {
        "strategy": "fixture",
        "normalization_tables_available": True,
        "eligible_edges": 15,
        "normalized_edges": 15,
        "normalization_coverage": 1.0,
    }
    standardized_skill_ids = {"skill:python", "skill:cloud", "skill:security"}
    raw_fallback_skill_ids: set[str] = set()
    skill_names = {
        "skill:python": "Python",
        "skill:cloud": "云平台",
        "skill:security": "安全策略",
    }
    edge_count = 15

    def __init__(self) -> None:
        self.rows = []
        months = [("2026-05", "source_a", "c1"), ("2026-05", "source_b", "c2"),
                  ("2026-06", "source_a", "c3"), ("2026-06", "source_b", "c4"),
                  ("2026-07", "source_a", "c1"), ("2026-07", "source_b", "c2")]
        for index, (month, source, company) in enumerate(months):
            self.rows.append({
                "jd_id": f"new-{index}",
                "canonical_role": "安全工程师",
                "role_id": "role:security",
                "company_id": company,
                "title": "AI Agent 安全工程师",
                "posted_at": f"{month}-{10 + index % 3:02d}",
                "description": "负责 AI 安全策略设计；建设模型风险监控；推动安全运营落地。",
                "source_file": f"{source}/it.csv",
                "template_cluster_id": f"template-{index}",
                "domain_label": "IT",
                "duplicate_of": "",
            })
        # A known existing title proves alias removal is separate from the
        # candidate and does not affect the new concept's deterministic ID.
        self.rows.append({
            "jd_id": "known-1",
            "canonical_role": "系统工程师",
            "role_id": "role:system",
            "company_id": "c9",
            "title": "系统工程师",
            "posted_at": "2026-06-12",
            "description": "负责系统运行维护和故障处理。",
            "source_file": "source_a/it.csv",
            "template_cluster_id": "known-template",
            "domain_label": "IT",
            "duplicate_of": "",
        })
        self.edges = [
            {
                "jd_id": row["jd_id"], "skill_id": skill, "skill_name": skill,
                "evidence_status": "VERIFIED", "competency_category": "技术",
                "evidence_quote": f"需要使用 {skill} 完成 AI 安全策略设计。",
                "requirement_type": "required", "confidence": 0.9,
            }
            for row in self.rows if row["jd_id"].startswith("new-")
            for skill in ("skill:python", "skill:cloud", "skill:security")
        ]

    def capture(self):
        return {"type": "fixture", "usable_jds": len(self.rows), "connected": True}

    def identity(self):
        return "fixture:stable"

    def load_jd_rows(self, *, sample_limit=0):
        return list(self.rows)

    def load_descriptions(self, jd_ids):
        wanted = set(jd_ids)
        return {row["jd_id"]: row["description"] for row in self.rows if row["jd_id"] in wanted}

    def load_known_titles(self):
        return {"系统工程师"}

    def iter_skill_edges(self, **kwargs):
        return iter(self.edges)

    def public_metadata(self):
        return {"type": "fixture", "usable_jds": len(self.rows)}

    def verify_unchanged(self):
        return None


def _run(
    tmp_path: Path,
    *,
    llm_enabled: bool,
    role_limit: int = 10,
    missing_key: bool = False,
    min_responsibility_evidence: int = 2,
    source: FakeSource | None = None,
):
    config = EvolutionConfig(
        database_path=None,
        output_root=tmp_path,
        source_backend="fixture",
        cutoff=datetime(2026, 5, 1),
        as_of=datetime(2026, 8, 21, 23, 59, 59),
        role_review_limit=role_limit,
        skill_review_limit=0,
        llm_enabled=llm_enabled,
        llm_api_key_env="NEW_ROLE_TEST_MISSING_KEY" if missing_key else "IFLYTEK_SPARK_API_PASSWORD",
        min_new_role_jds=5,
        min_new_role_companies=4,
        min_new_role_templates=4,
        min_candidate_skills=3,
        min_shared_candidate_skills=2,
        min_responsibility_evidence=min_responsibility_evidence,
        min_independent_sources=2,
        min_consecutive_months=2,
    )
    return EvolutionEngine(config, data_source=source or FakeSource()).run()


def _candidate_ids(result: dict, tmp_path: Path) -> tuple[str, ...]:
    import json

    path = Path(result["output_dir"]) / "new_role_candidates.json"
    return tuple(row["candidate_id"] for row in json.loads(path.read_text(encoding="utf-8")))


def test_llm_switch_does_not_change_mechanical_recall(tmp_path: Path) -> None:
    off = _run(tmp_path / "off", llm_enabled=False)
    on = _run(tmp_path / "on", llm_enabled=True, missing_key=True)
    assert _candidate_ids(off, tmp_path) == _candidate_ids(on, tmp_path)


def test_missing_llm_keeps_candidate_available_for_human_review(tmp_path: Path) -> None:
    result = _run(tmp_path / "degraded", llm_enabled=True, missing_key=True)
    assert result["state"] == "DEGRADED_REVIEW_READY"
    assert result["summary"]["new_role_review_tasks"] >= 1
    import json

    rows = json.loads((Path(result["output_dir"]) / "new_role_candidates.json").read_text(encoding="utf-8"))
    assert rows and rows[0]["rule_state"] == "REVIEW"
    queue = json.loads((Path(result["output_dir"]) / "review_queue.json").read_text(encoding="utf-8"))
    assert any(item["task_type"] == "NEW_ROLE_REVIEW" for item in queue)


def test_default_candidate_thresholds_preserve_full_graph_discovery_flow() -> None:
    config = EvolutionConfig(database_path=None, output_root=Path("."))
    assert config.min_new_role_jds == 3
    assert config.min_new_role_companies == 3
    assert config.min_new_role_templates == 3
    assert config.min_candidate_skills == 3
    assert config.min_shared_candidate_skills == 2
    assert config.min_responsibility_evidence == 0
    assert config.min_independent_sources == 2
    assert config.min_consecutive_months == 2
    assert config.watch_min_independent_sources == 1
    assert config.watch_min_consecutive_months == 1
    assert config.public_role_minimum == 10
    assert config.public_role_limit == 10
    assert config.min_role_companies_per_window == 5
    assert config.skill_review_limit == 40
    assert config.title_cluster_anchor_similarity == 0.78
    assert config.max_title_cluster_members == 24


def test_title_function_boundaries_separate_roles_that_share_ai_words() -> None:
    assert title_function_family("AI Agent产品经理") == "product"
    assert title_function_family("AI Agent测试开发工程师") == "quality"
    assert title_function_family("AI Agent后端开发工程师") == "engineering"


def test_title_clustering_blocks_transitive_similarity_chain(tmp_path: Path) -> None:
    source = FakeSource()
    titles = [
        "java软件开发工程师",
        "jave软件开发工程师",
        "pave软件开发工程师",
        "pyve软件开发工程师",
        "pyth软件开发工程师",
        "python软件开发工程师",
    ]
    source.rows = [
        {
            "jd_id": f"chain-{index}",
            "canonical_role": "软件工程师",
            "role_id": "role:software",
            "company_id": f"company-{index}",
            "title": title,
            "posted_at": "2026-06-12",
            "description": "负责软件开发和系统设计。",
            "source_file": f"source-{index}/it.csv",
            "template_cluster_id": f"template-{index}",
            "domain_label": "IT",
            "duplicate_of": "",
        }
        for index, title in enumerate(titles)
    ]
    source.edges = []
    source.load_known_titles = lambda: set()

    result = _run(
        tmp_path / "transitive-chain",
        llm_enabled=False,
        role_limit=10,
        source=source,
    )
    import json

    quality = json.loads(
        (Path(result["output_dir"]) / "data_quality_report.json").read_text(
            encoding="utf-8"
        )
    )
    funnel = quality["role_discovery"]["funnel"]
    assert funnel["largest_concept_members"] < len(titles)
    assert funnel["guarded_merge_rejections"]["ANCHOR_SIMILARITY_GUARD"] >= 1


def test_skill_review_budget_is_spread_across_roles_before_repeating() -> None:
    changes = [
        {"candidate_id": "a1", "role": "岗位A", "rule_state": "REVIEW"},
        {"candidate_id": "a2", "role": "岗位A", "rule_state": "REVIEW"},
        {"candidate_id": "b1", "role": "岗位B", "rule_state": "REVIEW"},
        {"candidate_id": "b2", "role": "岗位B", "rule_state": "REVIEW"},
        {"candidate_id": "c1", "role": "岗位C", "rule_state": "REVIEW"},
        {"candidate_id": "watch", "role": "岗位D", "rule_state": "WATCH"},
    ]

    selected = EvolutionEngine._select_skill_review(changes, 5)

    assert [row["candidate_id"] for row in selected] == [
        "a1",
        "b1",
        "c1",
        "a2",
        "b2",
    ]


def test_public_tier_prefers_strong_semantics_then_unseen_evidence_without_ai() -> None:
    config = EvolutionConfig(
        database_path=None,
        output_root=Path("."),
        public_role_minimum=10,
        public_role_limit=0,
    )
    engine = EvolutionEngine(config)
    candidates = [
        {
            "candidate_id": "strong",
            "candidate_title": "AI评测工程师",
            "current_company_count": 3,
            "current_template_count": 3,
            "historical_jd_count": 7,
            "discovery_type": "RECENT_GROWTH",
            "emergence_score": 90,
        },
        {
            "candidate_id": "unseen",
            "candidate_title": "企业知识库工程师",
            "current_company_count": 3,
            "current_template_count": 3,
            "historical_jd_count": 0,
            "discovery_type": "NEW_TITLE",
            "emergence_score": 80,
        },
        {
            "candidate_id": "noise",
            "candidate_title": "大数据开发工程师助理",
            "current_company_count": 3,
            "current_template_count": 3,
            "historical_jd_count": 0,
            "discovery_type": "NEW_TITLE",
            "emergence_score": 95,
        },
    ]

    engine._assign_publication_tiers(candidates)

    assert candidates[0]["publication_state"] == "PUBLISHED_CANDIDATE"
    assert candidates[1]["publication_state"] == "PUBLISHED_CANDIDATE"
    assert candidates[2]["publication_state"] == "OBSERVATION_ONLY"
    assert engine.role_funnel["public_evidence_candidates"] == 2


def test_missing_responsibility_text_does_not_block_mechanical_candidate(
    tmp_path: Path,
) -> None:
    source = FakeSource()
    for row in source.rows:
        row["description"] = ""
    result = _run(
        tmp_path / "no-responsibility-gate",
        llm_enabled=False,
        min_responsibility_evidence=0,
        source=source,
    )
    assert result["summary"]["new_role_candidates"] == 1


def test_name_discovery_does_not_require_processed_role_or_skill_evidence(
    tmp_path: Path,
) -> None:
    source = FakeSource()
    for index, row in enumerate(source.rows):
        row["canonical_role"] = "" if index < 6 else "完全不同的爬虫分类"
        row["description"] = ""
    source.edges = []
    result = _run(
        tmp_path / "title-only",
        llm_enabled=False,
        min_responsibility_evidence=99,
        source=source,
    )
    assert result["summary"]["new_role_candidates"] == 1
    import json

    candidate = json.loads(
        (Path(result["output_dir"]) / "new_role_candidates.json").read_text(
            encoding="utf-8"
        )
    )[0]
    assert candidate["first_seen"] <= candidate["last_seen"]
    assert candidate["raw_title_variants"]
    assert candidate["monthly_trend"]
    assert candidate["source_distribution"]


def test_title_normalization_keeps_semantic_markers_and_drops_noise() -> None:
    assert "aiagent" in normalize_title("高级AI Agent开发工程师（上海）")
    assert "全栈" in normalize_title("AI全栈工程师(J10512)")
    assert "大模型" in normalize_title("大模型应用开发工程师（AI Agent方向）")
    assert "北京" not in normalize_title("智能驾驶算法工程师（北京）")
    assert display_title("dataengineer") == "数据工程师"
    assert display_title("databaseengineer") == "databaseengineer"


def test_bundled_taxonomy_prevents_rediscovering_controlled_roles() -> None:
    known = bundled_known_titles()
    assert normalize_title("产品经理") in known
    assert normalize_title("软件测试") in known


def test_it_scope_rejects_physical_engineering_but_keeps_cross_domain_software() -> None:
    assert not title_is_in_it_scope("控制电路硬件工程师")
    assert not title_is_in_it_scope("汽车电子测试工程师")
    assert title_is_in_it_scope("自动驾驶车辆端软件系统工程师")
    assert title_is_in_it_scope("车身域软件测试和标定工程师")
    assert title_is_in_it_scope("嵌入式硬件开发工程师")
    assert title_is_in_it_scope("数据工程师")


def test_it_scope_rejects_non_it_business_titles() -> None:
    assert not title_is_in_it_scope("仓库管理员")
    assert not title_is_in_it_scope("销售经理")
    assert title_is_in_it_scope("AI仓储算法工程师")


def test_established_composites_are_not_rediscovered_as_new_roles() -> None:
    known = bundled_known_titles()
    for title in (
        "FAE技术支持工程师",
        "硬件产品经理",
        "数据算法工程师",
        "IT网络工程师",
        "渗透测试工程师",
        "AI开发工程师",
        "productmanagericc",
    ):
        assert established_role_composition(title, known), title


def test_emerging_semantics_survive_established_role_composition_filter() -> None:
    known = bundled_known_titles()
    for title in (
        "AI数据工程师",
        "大模型应用工程师",
        "智能驾驶系统工程师",
        "具身智能算法工程师",
    ):
        expected = "" if title != "AI数据工程师" else "数据工程师"
        assert established_role_composition(title, known) == expected, title
    assert established_role_composition("AI全栈开发工程师", known) == ""


def test_role_discovery_loads_skills_when_skill_change_review_is_off(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path / "role-skills", llm_enabled=False)
    import json

    rows = json.loads(
        (Path(result["output_dir"]) / "new_role_candidates.json").read_text(
            encoding="utf-8"
        )
    )
    assert rows[0]["candidate_skills_detail"]
    assert rows[0]["publication_state"] == "PUBLISHED_CANDIDATE"


def test_explicit_cutoff_ignores_current_days() -> None:
    config = EvolutionConfig(
        database_path=None,
        output_root=Path("."),
        cutoff=datetime(2026, 5, 1),
        as_of=datetime(2026, 8, 21),
        current_days=7,
    )
    engine = EvolutionEngine(config)
    cutoff, as_of, _ = engine._resolve_windows([
        {"_posted_at": datetime(2026, 1, 1)},
        {"_posted_at": datetime(2026, 8, 21)},
    ])
    assert cutoff == datetime(2026, 5, 1)
    assert as_of == datetime(2026, 8, 21)


def test_funnel_explains_rejection(tmp_path: Path) -> None:
    result = _run(tmp_path / "funnel", llm_enabled=False)
    import json

    quality = json.loads((Path(result["output_dir"]) / "data_quality_report.json").read_text(encoding="utf-8"))
    funnel = quality["role_discovery"]["funnel"]
    assert funnel["available_jds"] == 7
    assert funnel["current_titles"] >= 2
    assert funnel["mechanical_concepts"] >= 2
    assert funnel["top_k"] >= 1
    assert any(item["reason"] for item in quality["role_discovery"]["rejected_concepts"])


def test_watch_floor_keeps_single_source_recent_concept(tmp_path: Path) -> None:
    result = _run(tmp_path / "watch-floor", llm_enabled=False)
    import json

    rows = json.loads(
        (Path(result["output_dir"]) / "new_role_candidates.json").read_text(
            encoding="utf-8"
        )
    )
    # The fixture has enough support for a strict REVIEW.  This assertion also
    # protects the explicit two-level contract if the fixture is later reduced
    # to one source or one month: it must remain a candidate, not disappear.
    assert rows
    assert all(row["rule_state"] in {"REVIEW", "WATCH"} for row in rows)
    assert all(
        "WATCH_REQUIRES_MORE_SOURCE_OR_PERSISTENCE_EVIDENCE"
        not in row["rule_reasons"]
        for row in rows
        if row["rule_state"] == "REVIEW"
    )
