"""岗位名称归一化项目的公共接口。"""

from .config import (
    DEFAULT_CONFIG_PATH,
    MatchingThresholds,
    MatchingWeights,
    RoleNormalizerConfig,
    load_config,
)
from .models import (
    JobTitleRecord,
    MatchScores,
    ResolutionType,
    RoleDefinition,
    RoleResolution,
)
from .registry import RoleRegistry, normalize_lookup_key
from .discovery import NewRoleCandidate, NewRoleDiscovery
from .embedding import HashingTextEmbedder, SentenceTransformerEmbedder, TextEmbedder
from .resolver import ExistingRoleResolver
from .taxonomy_adapter import load_role_registry

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "JobTitleRecord",
    "MatchScores",
    "MatchingThresholds",
    "MatchingWeights",
    "NewRoleCandidate",
    "NewRoleDiscovery",
    "ResolutionType",
    "RoleDefinition",
    "RoleNormalizerConfig",
    "RoleRegistry",
    "RoleResolution",
    "ExistingRoleResolver",
    "HashingTextEmbedder",
    "SentenceTransformerEmbedder",
    "TextEmbedder",
    "load_role_registry",
    "load_config",
    "normalize_lookup_key",
]
