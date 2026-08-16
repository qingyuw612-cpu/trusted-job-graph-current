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
    late_arrival_grace_days: int = 7
    skill_change_source_family: str = ""

    focus_categories: tuple[str, ...] = ("技术", "知识")
    verified_status: str = "VERIFIED"
    min_role_companies_per_window: int = 8

    min_new_role_jds: int = 3
    min_new_role_companies: int = 3
    min_new_role_templates: int = 3
    min_candidate_skills: int = 3
    min_shared_candidate_skills: int = 2
    min_consecutive_months: int = 2
    min_independent_sources: int = 2
    min_source_companies: int = 2
    min_month_companies: int = 2
    concept_title_similarity: float = 0.78
    concept_skill_jaccard: float = 0.45
    max_rare_historical_jds: int = 2
    min_current_to_historical_ratio: float = 3.0
    new_role_jaccard_max: float = 0.40
    alias_jaccard_min: float = 0.70
    role_review_limit: int = 50

    min_skill_review_companies: int = 3
    min_skill_confirm_companies: int = 5
    min_skill_coverage: float = 0.10
    min_skill_confirm_coverage: float = 0.15
    min_skill_delta: float = 0.10
    q_value_threshold: float = 0.10
    skill_review_limit: int = 5

    source_drift_warning: float = 0.20
    industry_drift_warning: float = 0.20

    llm_enabled: bool = False
    llm_provider: str = "iflytek_maas_openai"
    llm_model: str = "xop3qwen1b7"
    # This is the complete chat-completions endpoint, not only the OpenAI base URL.
    llm_base_url: str = (
        "https://maas-api.cn-huabei-1.xf-yun.com/v2/chat/completions"
    )
    llm_api_key_env: str = "IFLYTEK_MAAS_API_KEY"
    # Deprecated compatibility option for earlier Spark Ultra configurations.
    llm_api_password_env: str = ""
    llm_search_disable: bool = True
    llm_json_mode: bool = False
    llm_temperature: float = 0.10
    llm_max_output_tokens: int = 1200
    llm_timeout_seconds: int = 60
    llm_max_requests: int = 50
    llm_max_retries: int = 1
    llm_max_evidence_per_candidate: int = 5
    llm_max_input_characters: int = 6000
    prompt_version: str = "role-evolution-v5-qwen-classify-evidence-assemble"

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
