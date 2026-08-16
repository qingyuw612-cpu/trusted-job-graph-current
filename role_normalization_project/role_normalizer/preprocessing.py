"""把已有的规则清洗器适配为岗位概念归一化的预处理步骤。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPOSITORY_DIR = Path(__file__).resolve().parents[2]
if str(REPOSITORY_DIR) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_DIR))

from trusted_graph_agent.job_title_normalizer import JobTitleNormalizer


class TitlePreprocessor:
    """执行可审计的规则清洗，但不在这一阶段决定最终受控岗位。"""

    def __init__(self, config: dict[str, Any] | None = None):
        """可选传入规则配置；默认复用项目当前岗位名称清洗配置。"""

        self.normalizer = JobTitleNormalizer(config=config)

    def normalize(self, title: str) -> tuple[str, tuple[str, ...]]:
        """返回清洗后的岗位名称和方向标签。"""

        result = self.normalizer.normalize(title)
        return result.normalized_name, result.tags
