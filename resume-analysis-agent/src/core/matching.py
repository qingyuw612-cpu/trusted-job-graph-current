"""子串规范化 + 命中搜索 — 纯函数，零外部依赖。

职责：
- 文本规范化（去空白/常见标点/全角转半角/小写）
- 单条 JD 要求 vs 候选人条目的多级模糊匹配
- 在简历原文中搜索 Role 核心技能命中（归一化子串包含）
"""

import re
from typing import Any, Dict, List, Sequence, Tuple

from .dimensions import CATEGORY_TO_DIM

# ==================== 匹配参数 ====================

_MIN_SUBSTR_LEN = 3          # 归一化后短语子串匹配的最短长度（中文 3 字符 ≈ 1 词）
_MIN_PHRASE_LEN = 4          # 滑窗直接命中长度（>=4 的连续子串视为可靠短语）
_MIN_CONTAIN_LEN = 3         # 整串包含匹配的最短长度
_MIN_EN_SUBSTR_LEN = 4       # 纯英文连续字母串的子串最短长度，避免 "ai"/"ic" 误命中
_OVERLAP_THRESHOLD = 0.75    # 字符重叠率兜底阈值

# 3 字符窗口命中时的通用后缀/前缀词黑名单：
# 避免 "博士学历" vs "本科硕士学历" 因共享 "士学历" 而误判命中
_GENERIC_WINDOW_WORDS = (
    "学历", "经验", "能力", "知识", "意识", "精神", "专业", "原理", "相关",
    "以上", "良好", "熟练", "熟悉", "掌握", "精通", "工程", "技术", "设计",
    "开发", "流程", "要求", "条件", "优先", "加分", "方向", "背景",
)

# 学历等级（用于任职条件特判：等级比较而非词法匹配）
_DEGREE_LEVELS = {"博士": 3, "硕士": 2, "研究生": 2, "本科": 1, "学士": 1, "大专": 0, "专科": 0}


def _is_degree_requirement(name: str) -> bool:
    """是否为学历等级要求（如"本科及以上学历"），需按等级语义判定而非子串匹配。"""
    if not name:
        return False
    has_degree_word = any(w in name for w in _DEGREE_LEVELS)
    has_degree_scope = any(k in name for k in ("学历", "以上", "以下"))
    return has_degree_word and has_degree_scope


# ==================== 文本规范化 ====================

def _normalize(text: str) -> str:
    """归一化：去空白、常见标点、全角转半角、转小写。"""
    t = text or ""
    t = t.replace("\u3000", " ").replace("\xa0", " ")
    # 全角转半角
    t = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in t)
    t = re.sub(r"[\s，。；、,.!?！？:：;；()（）\[\]【】/\\|~`·\-—_]+", "", t)
    return t.lower()


# ==================== 单条匹配 ====================

def _max_degree_level(text: str) -> int:
    """提取文本中的最高学历等级（博士3 > 硕士2 > 本科1 > 大专0，无则 -1）。"""
    level = -1
    for word, lv in _DEGREE_LEVELS.items():
        if word in text and lv > level:
            level = lv
    return level


def _degree_satisfied(cand: str, jd: str) -> bool:
    """任职条件学历特判：按等级比较，而非词法匹配。

    如 "本科及以上学历" vs "本科/硕士学历" → 满足；
    "博士学历" vs "本科/硕士学历" → 不满足。
    """
    cand_level = _max_degree_level(cand)
    jd_level = _max_degree_level(jd)
    if cand_level < 0 or jd_level < 0:
        return False
    if "及以上" in jd or "以上" in jd:
        return cand_level >= jd_level
    if "以下" in jd or "及以下" in jd:
        return cand_level <= jd_level
    return cand_level >= jd_level


def _item_match(candidate_item: str, jd_item: str) -> bool:
    """判断候选人条目是否覆盖 JD 条目（多级模糊匹配）。"""
    cand = _normalize(candidate_item)
    jd = _normalize(jd_item)
    if not cand or not jd:
        return False
    if cand == jd:
        return True
    # 1. 整串包含（浓缩摘录："模拟IC设计经验" ⊂ "5年以上模拟IC设计经验"）
    if len(cand) >= _MIN_CONTAIN_LEN and cand in jd:
        return True
    if len(jd) >= _MIN_CONTAIN_LEN and jd in cand:
        return True
    # 学历硬性否决：双方均含学历词但等级不满足 → 直接不匹配（如 硕士 vs 博士）
    if (
        _max_degree_level(cand) >= 0
        and _max_degree_level(jd) >= 0
        and not _degree_satisfied(cand, jd)
    ):
        return False
    shorter, longer = (cand, jd) if len(cand) <= len(jd) else (jd, cand)
    # 2. 短语滑窗：>=4 字符的连续子串命中 → 可靠短语
    for start in range(len(shorter) - _MIN_PHRASE_LEN + 1):
        if shorter[start:start + _MIN_PHRASE_LEN] in longer:
            return True
    # 3. 短语滑窗：3 字符连续子串命中，且窗口不含通用词（防 "士学历" 类误报）
    for start in range(len(shorter) - _MIN_SUBSTR_LEN + 1):
        window = shorter[start:start + _MIN_SUBSTR_LEN]
        if window in longer and not any(g in window for g in _GENERIC_WINDOW_WORDS):
            return True
    # 4. 英文/数字 token（>=4）跨文本命中
    for token in re.findall(r"[a-z0-9]+", shorter):
        if len(token) >= _MIN_EN_SUBSTR_LEN and token in longer:
            return True
    # 5. 任职条件学历特判（等级比较）
    if _degree_satisfied(cand, jd):
        return True
    # 6. 字符重叠率兜底（按较短文本计，容忍浓缩表述）
    #    仅对中文/混合文本生效；纯英文/数字串需完整子串包含，
    #    避免 "fpga" 与 "figma" 因共享 f/g/a 字符而误判匹配。
    if len(shorter) >= _MIN_SUBSTR_LEN:
        if re.fullmatch(r"[a-z0-9]+", shorter):
            return shorter in longer
        overlap = sum(1 for ch in shorter if ch in longer)
        if overlap / len(shorter) >= _OVERLAP_THRESHOLD:
            return True
    return False


# ==================== 原文技能命中搜索 ====================

def match_skills_in_text(
    raw_text: str,
    skills: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """在简历原文中搜索 Role 技能命中（归一化子串包含）。

    Args:
        raw_text: 简历 Markdown 原文
        skills: Role 技能列表 [{name, category, weight, rank}, ...]

    Returns:
        {"hit": [...], "miss": [...], "hit_count": N, "total": M,
         "by_dim": {dim: {"hit": [...], "miss": [...], "hit_count": N, "total": M}}}
    """
    norm_text = _normalize(raw_text)
    hits: List[Dict[str, Any]] = []
    misses: List[Dict[str, Any]] = []
    by_dim: Dict[str, Dict[str, Any]] = {}

    for sk in skills:
        name = sk.get("name", "").strip()
        if not name:
            continue
        dim = CATEGORY_TO_DIM.get(sk.get("category", ""))
        if not dim:
            continue
        by_dim.setdefault(dim, {"hit": [], "miss": [], "hit_count": 0, "total": 0})

        norm_name = _normalize(name)
        if _is_degree_requirement(name):
            # 学历等级语义判定：简历最高学历满足要求即命中
            # （如简历"本科" 命中"本科及以上学历"；"本科" 不命中"硕士及以上学历"）
            matched = _degree_satisfied(raw_text, name)
        else:
            matched = norm_name in norm_text if len(norm_name) >= 2 else False

        # 找到原文中的位置（用于高亮）
        positions: List[Tuple[int, int]] = []
        if matched:
            start = 0
            while True:
                idx = raw_text.lower().find(name.lower(), start)
                if idx == -1:
                    break
                positions.append((idx, idx + len(name)))
                start = idx + 1

        entry: Dict[str, Any] = {
            "name": name,
            "category": sk.get("category", ""),
            "dim": dim,
            "weight": sk.get("weight", 0.0),
        }
        if matched:
            entry["positions"] = positions
            hits.append(entry)
            by_dim[dim]["hit"].append(entry)
            by_dim[dim]["hit_count"] += 1
        else:
            misses.append(entry)
            by_dim[dim]["miss"].append(entry)
        by_dim[dim]["total"] += 1

    return {
        "hit": hits,
        "miss": misses,
        "hit_count": len(hits),
        "total": len(skills),
        "by_dim": by_dim,
    }


# ==================== 维度统计工具 ====================

def _dim_stats(
    candidate_items: Sequence[str],
    jd_items: Sequence[str],
) -> Tuple[int, int]:
    """统计单维度命中数：(matched_jd_count, jd_total)。"""
    jd_list = [str(i).strip() for i in jd_items or [] if str(i).strip()]
    cand_list = [str(i).strip() for i in candidate_items or [] if str(i).strip()]
    if not jd_list:
        return 0, 0
    if not cand_list:
        return 0, len(jd_list)
    matched = sum(1 for jd_item in jd_list if any(_item_match(c, jd_item) for c in cand_list))
    return matched, len(jd_list)


def dim_coverage(candidate_items: Sequence[str], jd_items: Sequence[str]) -> float:
    """单维度得分：JD 要求条目中被候选人覆盖的比例。

    JD 无要求（空列表）视为无差距，得 1.0；候选人为空则必为 0.0。
    """
    matched, total = _dim_stats(candidate_items, jd_items)
    return 1.0 if total == 0 else matched / total

