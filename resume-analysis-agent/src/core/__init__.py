"""纯逻辑层 — 零外部依赖，可单测。

- dimensions: 七维定义 + CATEGORY_TO_DIM/DIM_TO_CATEGORY + 7→5 投影 + DIM_LABELS
- matching: 子串规范化 + 命中搜索
- ranking: 覆盖率计算 + 少条目惩罚 + Role 排名
"""

