"""可配置、可审计的岗位名称归一化工具。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path(__file__).with_name("job_title_normalization_config.json")

# 每次匹配一层最内侧括号；循环执行后可以兼容少量嵌套括号。
BRACKET_PATTERN = re.compile(
    r"（(?P<cn>[^（）]*)）|\((?P<en>[^()]*)\)|"
    r"\[(?P<square>[^\[\]]*)\]|【(?P<book>[^【】]*)】"
)
CONTENT_SEPARATOR_PATTERN = re.compile(r"[，,、;；|｜/／]+")
TITLE_SEPARATOR_PATTERN = re.compile(r"[\s\-_—–/／|｜\\]+")
MULTISPACE_PATTERN = re.compile(r"\s+")
TRAILING_GENERIC_PATTERN = re.compile(r"(?:岗位|职位|招聘)$")
DIRECTION_SUFFIX_PATTERN = re.compile(
    r"^[0-9A-Za-z+#.\u4e00-\u9fff]{1,30}(?:方向|领域)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    """保存一次岗位名称归一化的稳定输出结构。"""

    original_name: str
    normalized_name: str
    tags: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """转换为可直接 JSON 序列化的字典。"""

        return {
            "original_name": self.original_name,
            "normalized_name": self.normalized_name,
            "tags": list(self.tags),
        }


def load_normalization_config(path: Path | str = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """从 JSON 文件读取词典和正则配置，便于后续独立扩展规则。"""

    config_path = Path(path)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload.get("synonym_map"), dict):
        raise ValueError("配置缺少 synonym_map 字典")
    return payload


def _term_pattern(terms: list[str]) -> re.Pattern[str] | None:
    """把词典安全编译为正则，并优先匹配较长词，避免短词提前截断。"""

    values = sorted({str(term).strip() for term in terms if str(term).strip()}, key=len, reverse=True)
    if not values:
        return None
    return re.compile("|".join(re.escape(value) for value in values), re.IGNORECASE)


def _compile_patterns(values: list[str]) -> tuple[re.Pattern[str], ...]:
    """编译配置中的正则列表，并在启动时尽早暴露错误表达式。"""

    return tuple(re.compile(value) for value in values)


def _compact_key(value: str) -> str:
    """生成同义词查找键；忽略空白和大小写，但不破坏 C++、.NET 等符号。"""

    return re.sub(r"\s+", "", value).casefold()


class JobTitleNormalizer:
    """按照外部 JSON 配置清洗岗位名称并提取方向标签。"""

    def __init__(self, config: dict[str, Any] | None = None):
        """初始化词典、正则和大小写无关的同义词索引。"""

        self.config = config or load_normalization_config()
        self.location_pattern = _term_pattern(self.config.get("location_terms", []))
        self.recruitment_type_pattern = _term_pattern(
            self.config.get("recruitment_type_terms", [])
        )
        self.level_term_pattern = _term_pattern(self.config.get("level_terms", []))
        self.recruitment_description_pattern = _term_pattern(
            self.config.get("recruitment_description_terms", [])
        )
        self.benefit_pattern = _term_pattern(self.config.get("benefit_terms", []))
        self.modifier_pattern = _term_pattern(self.config.get("modifier_terms", []))
        self.direction_pattern = _term_pattern(self.config.get("direction_keywords", []))
        self.role_anchor_pattern = _term_pattern(self.config.get("role_anchor_terms", []))
        self.salary_patterns = _compile_patterns(self.config.get("salary_patterns", []))
        self.level_patterns = _compile_patterns(self.config.get("level_patterns", []))
        self.job_code_patterns = _compile_patterns(self.config.get("job_code_patterns", []))
        self.synonym_map = {
            _compact_key(source): str(target).strip()
            for source, target in self.config["synonym_map"].items()
            if str(source).strip() and str(target).strip()
        }

    @staticmethod
    def _matches_any_pattern(value: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
        """判断文本是否命中任意一个配置正则。"""

        return any(pattern.search(value) for pattern in patterns)

    def _is_noise_fragment(self, value: str) -> bool:
        """判断括号片段是否只描述地区、招聘方式、等级、薪资或岗位编号。"""

        text = value.strip()
        if not text:
            return True
        term_patterns = (
            self.location_pattern,
            self.recruitment_type_pattern,
            self.level_term_pattern,
            self.recruitment_description_pattern,
            self.benefit_pattern,
        )
        if any(pattern and pattern.search(text) for pattern in term_patterns):
            return True
        return self._matches_any_pattern(text, self.salary_patterns + self.level_patterns + self.job_code_patterns)

    def _is_direction_fragment(self, value: str) -> bool:
        """判断括号片段是否包含岗位方向、技术方向或业务领域信息。"""

        text = value.strip()
        return bool(
            text
            and (
                DIRECTION_SUFFIX_PATTERN.fullmatch(text)
                or (self.direction_pattern and self.direction_pattern.search(text))
            )
        )

    def _classify_parenthetical(self, content: str, tags: list[str]) -> None:
        """拆分括号内容：丢弃噪声，将有效方向或未知补充信息保存为标签。"""

        fragments = [item.strip() for item in CONTENT_SEPARATOR_PATTERN.split(content) if item.strip()]
        if not fragments:
            return
        keep_unknown = bool(self.config.get("keep_unknown_parenthetical_as_tag", True))
        for fragment in fragments:
            # 括号中可能同时包含等级词和真实岗位，例如“初级助理硬件工程师”。
            # 因此先局部删除噪声，只有清理后为空才丢弃整个片段。
            cleaned = self._remove_inline_noise(fragment)
            cleaned = TITLE_SEPARATOR_PATTERN.sub(" ", cleaned)
            cleaned = MULTISPACE_PATTERN.sub(" ", cleaned).strip(" -_/,，、;；|｜")
            if not cleaned:
                continue
            if self._is_direction_fragment(cleaned) or keep_unknown:
                tags.append(cleaned)

    def _extract_parenthetical_tags(self, value: str, tags: list[str]) -> str:
        """遍历四类括号，删除括号本身并把非噪声内容提取到标签列表。"""

        text = value
        while True:
            matched = False

            def replace(match: re.Match[str]) -> str:
                nonlocal matched
                matched = True
                content = next((group for group in match.groups() if group is not None), "")
                self._classify_parenthetical(content, tags)
                return " "

            updated = BRACKET_PATTERN.sub(replace, text)
            text = updated
            if not matched:
                break
        return text

    def _remove_inline_noise(self, value: str) -> str:
        """删除岗位主名称中的等级、招聘修饰、薪资、编号及可选地区信息。"""

        text = value
        for pattern in self.salary_patterns + self.level_patterns + self.job_code_patterns:
            text = pattern.sub(" ", text)
        if self.modifier_pattern:
            text = self.modifier_pattern.sub(" ", text)
        if self.recruitment_type_pattern:
            text = self.recruitment_type_pattern.sub(" ", text)
        if self.config.get("remove_inline_locations", True) and self.location_pattern:
            text = self.location_pattern.sub(" ", text)
        return text

    def _extract_inline_direction_tags(self, value: str, tags: list[str]) -> str:
        """提取独立或黏连的方向后缀，同时避免把完整岗位误当成标签。"""

        tokens = [item for item in MULTISPACE_PATTERN.split(value.strip()) if item]
        core_tokens: list[str] = []
        for token in tokens:
            cleaned = token.strip("()（）[]【】,，;；")
            direction_match = DIRECTION_SUFFIX_PATTERN.fullmatch(cleaned)
            split_done = False

            # 处理“数据分析师商业方向”这类没有分隔符的写法：以最后一个岗位锚点
            # 为界拆成“数据分析师”与“商业方向”。仅有“方向”二字时不强拆。
            if direction_match and self.role_anchor_pattern:
                anchors = list(self.role_anchor_pattern.finditer(cleaned))
                if anchors:
                    split_at = anchors[-1].end()
                    core = cleaned[:split_at].strip()
                    direction = cleaned[split_at:].strip()
                    if core and direction not in {"方向", "领域"} and DIRECTION_SUFFIX_PATTERN.fullmatch(direction):
                        core_tokens.append(core)
                        tags.append(direction)
                        split_done = True

            if split_done:
                continue
            # 只有在方向本身由分隔符独立出来时才移入标签；单独一个
            # “人工智能方向”保留为名称，防止主岗位被清空。
            if direction_match and len(tokens) > 1:
                tags.append(cleaned)
            else:
                core_tokens.append(cleaned)
        return " ".join(item for item in core_tokens if item)

    def _recover_core_from_tags(self, tags: list[str]) -> str:
        """主名称为空时，从含明确岗位锚点的括号内容中恢复岗位名称。"""

        if not self.role_anchor_pattern:
            return ""
        for index, tag in enumerate(tags):
            if self.role_anchor_pattern.search(tag):
                return tags.pop(index)
        return ""

    def _apply_synonym(self, value: str) -> str:
        """使用大小写无关的精确别名映射，避免子串替换误伤其他岗位。"""

        compact = _compact_key(value)
        return self.synonym_map.get(compact, value)

    @staticmethod
    def _deduplicate_tags(values: list[str]) -> tuple[str, ...]:
        """按出现顺序去重标签，英文标签比较时忽略大小写。"""

        output: list[str] = []
        seen: set[str] = set()
        for value in values:
            cleaned = MULTISPACE_PATTERN.sub(" ", value).strip(" -_/,，、;；|｜")
            key = cleaned.casefold()
            if cleaned and key not in seen:
                seen.add(key)
                output.append(cleaned)
        return tuple(output)

    def normalize(self, job_name: str) -> NormalizationResult:
        """执行完整归一化，返回原名称、标准岗位名称和方向标签。"""

        original = str(job_name or "").strip()
        if not original:
            return NormalizationResult(original_name="", normalized_name="", tags=())

        tags: list[str] = []
        text = self._extract_parenthetical_tags(original, tags)
        # 薪资中的横杠也是有效结构（如 10-20K），必须先整体删除噪声，
        # 再把剩余横杠等符号统一为空格。
        text = self._remove_inline_noise(text)
        text = TITLE_SEPARATOR_PATTERN.sub(" ", text)
        text = MULTISPACE_PATTERN.sub(" ", text).strip(" -_/,，、;；|｜")
        text = self._extract_inline_direction_tags(text, tags)
        text = MULTISPACE_PATTERN.sub(" ", text).strip()
        text = TRAILING_GENERIC_PATTERN.sub("", text).strip()
        if not text:
            text = self._recover_core_from_tags(tags)
        normalized = self._apply_synonym(text)

        return NormalizationResult(
            original_name=original,
            normalized_name=normalized,
            tags=self._deduplicate_tags(tags),
        )


_DEFAULT_NORMALIZER: JobTitleNormalizer | None = None


def normalize_job_title(job_name: str) -> dict[str, Any]:
    """提供简单函数式入口，适合在爬虫、ETL 或接口中直接调用。"""

    global _DEFAULT_NORMALIZER
    if _DEFAULT_NORMALIZER is None:
        _DEFAULT_NORMALIZER = JobTitleNormalizer()
    return _DEFAULT_NORMALIZER.normalize(job_name).to_dict()


def normalize_job_title_json(job_name: str) -> str:
    """返回中文不转义的 JSON 字符串。"""

    return json.dumps(normalize_job_title(job_name), ensure_ascii=False, indent=2)


if __name__ == "__main__":
    samples = [
        "高级Java开发工程师（北京）",
        "产品经理（实习生）",
        "AI算法工程师（大模型方向）",
        "软件开发工程师-后端方向",
        "JAVA开发工程师",
    ]
    for sample in samples:
        print(normalize_job_title_json(sample))
