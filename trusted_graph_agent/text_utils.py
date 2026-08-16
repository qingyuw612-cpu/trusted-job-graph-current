from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path


EMPTY_VALUES = {"", "nan", "none", "null", "n/a", "-"}


def clean_value(value: object) -> str:
    text = str(value or "").replace("\ufeff", "").strip()
    return "" if text.lower() in EMPTY_VALUES else text


def clean_header(value: str) -> str:
    return clean_value(value).lstrip("\ufeff")


def normalize_text(text: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff+#.]", "", clean_value(text).lower())


def stable_id(prefix: str, *parts: str, length: int = 16) -> str:
    raw = "|".join(clean_value(part) for part in parts)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}:{digest}"


def parse_datetime(value: str) -> datetime | None:
    text = clean_value(value)
    if not text:
        return None
    normalized = text.replace("/", "-").replace("年", "-").replace("月", "-").replace("日", " ")
    try:
        return datetime.fromisoformat(normalized.strip())
    except ValueError:
        pass
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y-%m"):
        try:
            return datetime.strptime(normalized.strip(), pattern)
        except ValueError:
            continue
    return None


def quarter_window(value: datetime | None, fallback: datetime) -> tuple[str, str]:
    date = value or fallback
    quarter = (date.month - 1) // 3 + 1
    start_month = (quarter - 1) * 3 + 1
    return f"{date.year}Q{quarter}", f"{date.year:04d}-{start_month:02d}-01"


def time_decay(value: datetime | None, reference: datetime, half_life_months: float) -> float:
    if value is None:
        return 1.0
    age_days = max(0, (reference - value).total_seconds() / 86400)
    age_months = age_days / 30.4375
    return 0.5 ** (age_months / max(half_life_months, 0.1))


def text_hash(company: str, role: str, description: str) -> str:
    raw = "|".join((normalize_text(company), normalize_text(role), normalize_text(description)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _shingles(text: str, width: int = 3, limit: int = 12000) -> list[str]:
    normalized = normalize_text(text)[:limit]
    if len(normalized) <= width:
        return [normalized] if normalized else []
    return [normalized[index : index + width] for index in range(len(normalized) - width + 1)]


def simhash64(text: str) -> int:
    vector = [0] * 64
    for token in _shingles(text):
        digest = int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big")
        for bit in range(64):
            vector[bit] += 1 if digest & (1 << bit) else -1
    result = 0
    for bit, score in enumerate(vector):
        if score >= 0:
            result |= 1 << bit
    return result


def simhash_similarity(left: int, right: int) -> float:
    return 1.0 - ((left ^ right).bit_count() / 64.0)


def hybrid_similarity(left_text: str, right_text: str, left_hash: int, right_hash: int) -> float:
    rough = simhash_similarity(left_hash, right_hash)
    if rough < 0.78:
        return rough
    left = normalize_text(left_text)[:10000]
    right = normalize_text(right_text)[:10000]
    sequence = SequenceMatcher(None, left, right, autojunk=False).ratio()
    return 0.55 * rough + 0.45 * sequence


def simhash_bands(value: int, band_bits: int = 16) -> tuple[int, ...]:
    mask = (1 << band_bits) - 1
    return tuple((value >> offset) & mask for offset in range(0, 64, band_bits))


def infer_level(title: str, experience: str, description: str) -> str:
    text = f"{title} {experience} {description[:600]}".lower()
    if re.search(r"实习|intern|应届|校招|在校", text):
        return "实习/应届"
    if re.search(r"总监|负责人|经理岗|主管|团队管理|部门管理|技术经理|研发经理", text):
        return "管理岗"
    if re.search(r"专家|资深|架构师|首席|principal|staff", text):
        return "专家"
    if re.search(r"高级|senior|5年以上|5-10年|8年以上|十年以上", text):
        return "高级"
    years = [int(item) for item in re.findall(r"(\d+)\s*年", text)]
    if years:
        minimum = min(years)
        if minimum >= 5:
            return "高级"
        if minimum >= 3:
            return "中级"
        return "初级"
    if re.search(r"初级|助理|专员|1年|2年|1-3年|1—3年", text):
        return "初级"
    if re.search(r"中级|3年|4年|3-5年|3—5年", text):
        return "中级"
    return "未注明"


ROLE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"大模型|llm|语言模型"), "大模型算法工程师"),
    (re.compile(r"自然语言|nlp"), "自然语言处理工程师"),
    (re.compile(r"计算机视觉|机器视觉|图像算法|视觉算法|图像识别"), "计算机视觉工程师"),
    (re.compile(r"机器学习"), "机器学习工程师"),
    (re.compile(r"深度学习"), "深度学习工程师"),
    (re.compile(r"算法"), "算法工程师"),
    (re.compile(r"python.*(开发|工程师)|(开发|工程师).*python"), "Python开发工程师"),
    (re.compile(r"java.*(开发|工程师)|(开发|工程师).*java"), "Java开发工程师"),
    (re.compile(r"golang|go语言"), "Go开发工程师"),
    (re.compile(r"c\+\+|cpp"), "C++开发工程师"),
    (re.compile(r"\.net|c#"), ".NET开发工程师"),
    (re.compile(r"后端|服务端"), "后端开发工程师"),
    (re.compile(r"前端"), "前端开发工程师"),
    (re.compile(r"数据产品"), "数据产品经理"),
    (re.compile(r"平台产品"), "平台产品经理"),
    (re.compile(r"产品助理|产品专员"), "产品助理"),
    (re.compile(r"产品经理|产品主管"), "产品经理"),
    (re.compile(r"数据分析"), "数据分析师"),
    (re.compile(r"数据建模"), "数据建模工程师"),
    (re.compile(r"etl"), "ETL开发工程师"),
    (re.compile(r"bi工程师|商业智能"), "BI工程师"),
    (re.compile(r"自动化测试|测试开发"), "自动化测试工程师"),
    (re.compile(r"软件测试|测试工程师"), "软件测试工程师"),
    (re.compile(r"运维"), "运维工程师"),
    (re.compile(r"网络工程师|网络运维"), "网络工程师"),
    (re.compile(r"嵌入式软件"), "嵌入式软件工程师"),
    (re.compile(r"嵌入式硬件"), "嵌入式硬件工程师"),
    (re.compile(r"硬件测试"), "硬件测试工程师"),
    (re.compile(r"硬件工程师|硬件开发"), "硬件工程师"),
]


def normalize_role_title(title: str, fallback: str) -> str:
    text = clean_value(title)
    normalized = normalize_text(text)
    for pattern, canonical in ROLE_RULES:
        if pattern.search(normalized):
            return canonical
    cleaned = re.sub(r"[（(].*?[）)]", "", text)
    cleaned = re.sub(r"高级|资深|初级|中级|专家|实习|急聘|诚聘|招聘", "", cleaned)
    cleaned = re.sub(r"\s+", "", cleaned).strip("-_/·,，。 ")
    if 2 <= len(cleaned) <= 24:
        return cleaned
    return Path(fallback).stem


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
        return default if math.isnan(number) else number
    except (TypeError, ValueError):
        return default
