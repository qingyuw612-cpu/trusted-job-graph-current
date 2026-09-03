from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class EvolutionConfig:
    database_path: Path | None
    output_root: Path
    source_backend: str = "sqlite"
    neo4j_config_path: Path | None = None
    cutoff: datetime | None = None
    as_of: datetime | None = None
    baseline_days: int = 0
    current_days: int = 30
    # 0 表示全量；大于 0 时按最近采集时间取样，仅用于快速演示/在线预览。
    sample_limit: int = 0
    late_arrival_grace_days: int = 7
    skill_change_source_family: str = ""

    focus_categories: tuple[str, ...] = ("技术", "知识")
    verified_status: str = "VERIFIED"
    # 旧岗位能力变化至少需要基线、当前窗口各 5 家企业；单项能力仍需
    # 3 家企业、10 个百分点变化和 FDR 校正，扩大岗位覆盖但不放松
    # 单项变化的统计证据。
    min_role_companies_per_window: int = 5

    # 沿用既有全量发现流程：名称、职责和能力证据先通过机械门槛，
    # 再由 AI 补充建议、人工完成最终审核。AI 不承担候选准入或淘汰。
    min_new_role_jds: int = 3
    min_new_role_companies: int = 3
    min_new_role_templates: int = 3
    min_candidate_skills: int = 3
    min_shared_candidate_skills: int = 2
    # 职责原文仅作为 AI 与人工审核材料，不作为机械候选准入门槛。
    min_responsibility_evidence: int = 0
    min_consecutive_months: int = 2
    min_independent_sources: int = 2
    # WATCH 沿用原流程：来源或持续月份至少有一项达到原机械门槛。
    # 单来源/单月可以进入观察池，但不能因此进入正式图谱；公开层仍要求
    # 多企业、多模板、职责/技能证据及新兴语义或 AI 判断。
    watch_min_consecutive_months: int = 1
    watch_min_independent_sources: int = 1
    min_source_companies: int = 2
    min_month_companies: int = 2
    concept_title_similarity: float = 0.78
    concept_skill_jaccard: float = 0.45
    # Prevent single-linkage chaining (A≈B, B≈C, ...), which can otherwise
    # turn unrelated job titles into one giant connected component.
    title_cluster_anchor_similarity: float = 0.78
    max_title_cluster_members: int = 24
    max_rare_historical_jds: int = 2
    min_current_to_historical_ratio: float = 3.0
    new_role_jaccard_max: float = 0.40
    alias_jaccard_min: float = 0.70
    role_review_limit: int = 50
    # 对外岗位涌现榜至少提供这个数量的证据候选；默认以证据排序取满
    # 10 条，若数据不足则只展示实际通过机械门槛的数量。
    # 公开资格仍由机械规则决定，不能用补足数量的方式放行噪声。
    public_role_minimum: int = 10
    public_role_limit: int = 10
    min_public_role_companies: int = 3
    min_public_role_templates: int = 3
    min_public_responsibility_evidence: int = 2
    min_public_candidate_skills: int = 2
    min_public_llm_confidence: float = 0.50

    min_skill_review_companies: int = 3
    min_skill_confirm_companies: int = 5
    min_skill_coverage: float = 0.10
    min_skill_confirm_coverage: float = 0.15
    min_skill_delta: float = 0.10
    q_value_threshold: float = 0.10
    skill_review_limit: int = 40

    source_drift_warning: float = 0.20
    industry_drift_warning: float = 0.20

    llm_enabled: bool = True
    llm_provider: str = "iflytek_spark_openai"
    llm_model: str = "lite"
    # Spark OpenAI-compatible endpoint; Lite uses a single user message.
    llm_base_url: str = "https://spark-api-open.xf-yun.com/v1/chat/completions"
    llm_api_key_env: str = "IFLYTEK_SPARK_API_PASSWORD"
    # Compatibility with the previous MaaS credential during migration.
    llm_api_password_env: str = ""
    llm_search_disable: bool = True
    # Spark Lite 的 JSON Object Mode 在实测中可能返回空对象；使用简短四字段协议并本地校验。
    llm_json_mode: bool = False
    llm_temperature: float = 0.10
    llm_max_output_tokens: int = 1200
    llm_timeout_seconds: int = 60
    # 每个岗位最多使用两次请求：一次边界分类、一次证据约束的岗位定义生成。
    llm_max_requests: int = 110
    llm_max_retries: int = 1
    llm_max_evidence_per_candidate: int = 5
    llm_max_input_characters: int = 6000
    # LLM 只审核机械召回的 Top-K；名称聚类永远不占用 LLM 预算。
    llm_role_max_requests: int = 100
    llm_skill_max_requests: int = 40
    prompt_version: str = "role-evolution-v18-archived-jd-evidence"

    dry_run: bool = True

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["database_path"] = (
            str(self.database_path) if self.database_path is not None else None
        )
        data["output_root"] = str(self.output_root)
        data["neo4j_config_path"] = (
            str(self.neo4j_config_path)
            if self.neo4j_config_path is not None
            else None
        )
        data["cutoff"] = self.cutoff.isoformat(timespec="seconds") if self.cutoff else None
        data["as_of"] = self.as_of.isoformat(timespec="seconds") if self.as_of else None
        data["focus_categories"] = list(self.focus_categories)
        return data
