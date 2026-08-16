"""可信岗位能力图谱构建 Agent。

构建管线依赖科学计算组件，服务端只读 Neo4j 时不应被迫加载这些依赖。
"""

from __future__ import annotations

from typing import Any


__all__ = ["AgentConfig", "TrustedGraphAgent"]


def __getattr__(name: str) -> Any:
    if name == "AgentConfig":
        from .config import AgentConfig

        return AgentConfig
    if name == "TrustedGraphAgent":
        from .pipeline import TrustedGraphAgent

        return TrustedGraphAgent
    raise AttributeError(name)
