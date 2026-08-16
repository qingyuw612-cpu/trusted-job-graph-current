from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class AgentConfig:
    input_dir: Path
    output_dir: Path
    max_rows_per_file: int = 300
    scan_rows_per_file: int = 0
    group_by_file_role: bool = False
    it_only: bool = False
    half_life_months: float = 12.0
    template_similarity: float = 0.97
    extracted_entry_min_length: int = 30
    template_weight_floor: float = 0.35
    required_support_threshold: float = 0.60
    common_support_threshold: float = 0.30
    preferred_support_threshold: float = 0.10
    min_required_companies: float = 3.0
    related_min_cooccurrence: int = 2
    related_min_score: float = 0.12
    panorama_skill_limit: int = 10
    include_patterns: list[str] = field(default_factory=list)
    llm_endpoint: str = ""
    llm_timeout_seconds: int = 45

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["input_dir"] = str(self.input_dir)
        data["output_dir"] = str(self.output_dir)
        return data
