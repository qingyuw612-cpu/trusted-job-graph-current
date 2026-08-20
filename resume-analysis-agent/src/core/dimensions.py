"""维度定义 — 七维画像 key、类别映射、权重、中文标签。

核心口径为 **7 维**，严格对齐 Neo4j 图谱 NormalizedSkill.category：
    知识 / 技术 / 任职条件 / 招聘偏好 / 动机 / 特质 / 自我概念

挑战杯大纲的"五分类"（知识/技术/动机/特质/自我概念）仅为原始数据/汇报口径，
对外材料如需五分类，请用 project_to_five_dim() 做 7→5 投影。
"""

# 七维 key 顺序
DIMENSION_KEYS = (
    "knowledge",
    "skill",
    "qualifications",
    "preference",
    "motivation",
    "trait",
    "self_concept",
)

# NormalizedSkill.category → 七维画像 key
CATEGORY_TO_DIM = {
    "知识": "knowledge",
    "技术": "skill",
    "任职条件": "qualifications",
    "招聘偏好": "preference",
    "动机": "motivation",
    "特质": "trait",
    "自我概念": "self_concept",
}

# 七维画像 key → NormalizedSkill.category 反查表
DIM_TO_CATEGORY = {dim: category for category, dim in CATEGORY_TO_DIM.items()}

# 大纲五分类 key（挑战杯大纲 1.0：知识/技术/动机/特质/自我概念），
# 仅供原始数据/汇报口径的对外材料使用，不参与核心匹配。
OUTLINE_FIVE_KEYS = (
    "knowledge",
    "skill",
    "motivation",
    "trait",
    "self_concept",
)

# 七维 → 大纲五分类 投影映射：
# - 任职条件（学历/专业/经验门槛）归入"知识"（背景资质类）；
# - 招聘偏好（岗位倾向/意愿类表述）归入"动机"。
DIM_TO_OUTLINE = {
    "knowledge": "knowledge",
    "skill": "skill",
    "qualifications": "knowledge",
    "preference": "motivation",
    "motivation": "motivation",
    "trait": "trait",
    "self_concept": "self_concept",
}

# 默认权重（七维）
DEFAULT_WEIGHTS = {
    "knowledge": 0.22,
    "skill": 0.22,
    "qualifications": 0.18,
    "preference": 0.10,
    "motivation": 0.10,
    "trait": 0.09,
    "self_concept": 0.09,
}

# 中文标签（雷达图 / 报告展示用）
DIM_LABELS = {
    "knowledge": "知识",
    "skill": "技术",
    "qualifications": "任职条件",
    "preference": "招聘偏好",
    "motivation": "动机",
    "trait": "特质",
    "self_concept": "自我概念",
}


def project_to_five_dim(
    data: dict,
    mapping: dict | None = None,
) -> dict:
    """把七维画像投影为大纲五分类（仅对外材料/汇报口径用，不参与核心匹配）。

    Args:
        data: 七维画像，形如 {"knowledge": [...], "skill": [...], ...}，
              条目可为字符串或任意对象。
        mapping: 可选覆盖 七维 key → 五分类 key；缺省用 DIM_TO_OUTLINE。

    Returns:
        五分类画像 {"knowledge": [...], "skill": [...], "motivation": [...],
        "trait": [...], "self_concept": [...]}，顺序固定为 OUTLINE_FIVE_KEYS。
    """
    proj = mapping or DIM_TO_OUTLINE
    out = {five: [] for five in OUTLINE_FIVE_KEYS}
    for dim in DIMENSION_KEYS:
        five = proj.get(dim)
        if five not in out:
            continue
        items = data.get(dim) or []
        if items:
            out[five].extend(items)
    return out

