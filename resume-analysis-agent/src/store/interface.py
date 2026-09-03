"""RoleStore 抽象接口 — 数据访问与业务逻辑解耦。"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class RoleStore(ABC):
    """标准职业（Role）数据访问接口。

    Role 数据契约（每个元素）：
        {
            "role_name": str,
            "family_name": str,
            "domain_name": str,
            "jd_count": int,
            "skills": [
                {"name": str, "category": str, "weight": float, "rank": int},
                ...
            ],
        }
    """

    @abstractmethod
    def get_all_roles(self) -> List[Dict[str, Any]]:
        """加载全部 Role 及其核心技能。"""

    @abstractmethod
    def get_role_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """按角色名精确查找单个 Role，不存在返回 None。"""

