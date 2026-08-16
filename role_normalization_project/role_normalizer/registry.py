"""受控岗位注册表及精确名称、别名匹配。"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

from .models import ResolutionType, RoleDefinition


def normalize_lookup_key(value: str) -> str:
    """生成用于精确查询的稳定键，不擅自删除有语义的标点。"""

    normalized = unicodedata.normalize("NFKC", str(value)).strip().casefold()
    return re.sub(r"\s+", "", normalized)


class RoleRegistry:
    """只读受控岗位集合，负责确定性的规范名和别名查询。"""

    def __init__(self, roles: Iterable[RoleDefinition]) -> None:
        """建立岗位索引，并检测跨岗位名称冲突。"""

        self._roles: Dict[str, RoleDefinition] = {}
        self._name_index: Dict[str, Tuple[str, ResolutionType]] = {}
        for role in roles:
            self._add_role(role)

    def _add_role(self, role: RoleDefinition) -> None:
        """校验并加入一个岗位；任何歧义均在加载阶段报错。"""

        if not role.role_id:
            raise ValueError("role_id 不能为空")
        if not role.canonical_name:
            raise ValueError(f"岗位 {role.role_id} 的 canonical_name 不能为空")
        if role.role_id in self._roles:
            raise ValueError(f"重复的 role_id：{role.role_id}")

        entries = [(role.canonical_name, ResolutionType.EXACT)]
        entries.extend((alias, ResolutionType.ALIAS) for alias in role.aliases if alias)
        local_keys: set[str] = set()
        for name, resolution_type in entries:
            key = normalize_lookup_key(name)
            if not key:
                raise ValueError(f"岗位 {role.role_id} 包含空名称或空别名")
            if key in local_keys:
                continue
            local_keys.add(key)
            existing = self._name_index.get(key)
            if existing is not None and existing[0] != role.role_id:
                raise ValueError(
                    f"岗位名称/别名冲突：{name!r} 同时属于 "
                    f"{existing[0]} 和 {role.role_id}"
                )

        self._roles[role.role_id] = role
        for name, resolution_type in entries:
            key = normalize_lookup_key(name)
            if key not in self._name_index:
                self._name_index[key] = (role.role_id, resolution_type)

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "RoleRegistry":
        """从包含 ``roles`` 或 ``role_registry.roles`` 的字典加载。"""

        roles_data = data.get("roles")
        if roles_data is None:
            registry_data = data.get("role_registry", {})
            if not isinstance(registry_data, dict):
                raise ValueError("role_registry 必须是 JSON 对象")
            roles_data = registry_data.get("roles", [])
        if not isinstance(roles_data, list):
            raise ValueError("roles 必须是 JSON 数组")
        return cls(RoleDefinition.from_dict(item) for item in roles_data)

    @classmethod
    def from_json(cls, path: str | Path) -> "RoleRegistry":
        """从 UTF-8 JSON 文件加载受控岗位注册表。"""

        with Path(path).open("r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            raise ValueError("注册表 JSON 顶层必须是对象")
        return cls.from_dict(data)

    def get(self, role_id: str) -> Optional[RoleDefinition]:
        """按 role_id 查询岗位；不存在时返回 ``None``。"""

        return self._roles.get(role_id)

    def require(self, role_id: str) -> RoleDefinition:
        """按 role_id 查询岗位；不存在时抛出明确异常。"""

        try:
            return self._roles[role_id]
        except KeyError as exc:
            raise KeyError(f"未知 role_id：{role_id}") from exc

    def match_exact(
        self, name: str
    ) -> Optional[Tuple[RoleDefinition, ResolutionType]]:
        """精确匹配规范名或别名，返回岗位及匹配类型。"""

        matched = self._name_index.get(normalize_lookup_key(name))
        if matched is None:
            return None
        role_id, resolution_type = matched
        return self._roles[role_id], resolution_type

    def __iter__(self) -> Iterator[RoleDefinition]:
        """按照 role_id 排序稳定迭代，避免 JSON 顺序影响结果。"""

        for role_id in sorted(self._roles):
            yield self._roles[role_id]

    def __len__(self) -> int:
        """返回受控岗位数量。"""

        return len(self._roles)

    def to_dict(self) -> Dict[str, List[Dict[str, object]]]:
        """按稳定顺序导出整个注册表。"""

        return {"roles": [role.to_dict() for role in self]}
