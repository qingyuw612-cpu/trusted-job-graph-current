from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


def model_dict(value: Any) -> dict[str, Any]:
    data = asdict(value)
    for key, item in list(data.items()):
        if isinstance(item, datetime):
            data[key] = item.isoformat(timespec="seconds")
    return data


@dataclass(slots=True)
class JobDocument:
    jd_id: str
    source_file: str
    source_category: str
    raw_job_id: str
    company_id: str
    company_name: str
    title: str
    canonical_role: str
    description: str
    tags: str
    ability_analysis: str
    industry: str
    education: str
    experience: str
    salary: str
    location: str
    posted_at: datetime | None
    level: str
    exact_hash: str
    simhash: int
    duplicate_of: str = ""
    duplicate_reason: str = ""
    template_cluster_id: str = ""
    template_weight: float = 1.0
    time_weight: float = 1.0

    @property
    def is_duplicate(self) -> bool:
        return bool(self.duplicate_of)

    @property
    def evidence_text(self) -> str:
        return "\n".join(part for part in (self.description, self.tags) if part)


@dataclass(slots=True)
class SkillCandidate:
    skill_name: str
    raw_term: str
    requirement_type: str
    evidence_quote: str
    confidence: float
    source: str
    competency_category: str = ""
    tech_stack: str = ""


@dataclass(slots=True)
class SkillEvidence:
    jd_id: str
    skill_id: str
    skill_name: str
    raw_term: str
    requirement_type: str
    evidence_quote: str
    evidence_status: str
    confidence: float
    source: str
    competency_category: str
    tech_stack: str


@dataclass(slots=True)
class RoleProfile:
    profile_id: str
    role_id: str
    role_name: str
    industry_id: str
    industry_name: str
    level_id: str
    level_name: str
    time_window: str
    window_start: str
    jd_count: int
    company_count: int


@dataclass(slots=True)
class RoleSkillEdge:
    edge_id: str
    profile_id: str
    role_id: str
    skill_id: str
    relation: str
    tier: str
    jd_support: float
    company_support: float
    adjusted_support: float
    company_count: int
    effective_company_count: float
    evidence_count: int
    preferred_mentions: int


@dataclass(slots=True)
class ReviewTask:
    task_id: str
    jd_id: str
    skill_id: str
    skill_name: str
    reason: str
    evidence_status: str
    confidence: float
    evidence_quote: str
    status: str = "PENDING"
    decision: str = ""


@dataclass(slots=True)
class GraphBundle:
    run: dict[str, Any]
    role_families: list[dict[str, Any]] = field(default_factory=list)
    role_aliases: list[dict[str, Any]] = field(default_factory=list)
    role_relations: list[dict[str, Any]] = field(default_factory=list)
    roles: list[dict[str, Any]] = field(default_factory=list)
    role_profiles: list[dict[str, Any]] = field(default_factory=list)
    skills: list[dict[str, Any]] = field(default_factory=list)
    industries: list[dict[str, Any]] = field(default_factory=list)
    levels: list[dict[str, Any]] = field(default_factory=list)
    time_windows: list[dict[str, Any]] = field(default_factory=list)
    companies: list[dict[str, Any]] = field(default_factory=list)
    jds: list[dict[str, Any]] = field(default_factory=list)
    role_skill_edges: list[dict[str, Any]] = field(default_factory=list)
    role_skill_snapshots: list[dict[str, Any]] = field(default_factory=list)
    jd_skill_edges: list[dict[str, Any]] = field(default_factory=list)
    related_skill_edges: list[dict[str, Any]] = field(default_factory=list)
    evolution_edges: list[dict[str, Any]] = field(default_factory=list)
    review_tasks: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
