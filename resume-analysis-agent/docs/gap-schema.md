# 结构化 gap 响应 schema（B2）

`/gap` 返回中的 `report` 字段为前端可直接渲染的结构化契约，无需解析 Markdown。

## 顶层字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `role_name` | str | 目标岗位名 |
| `match` | object | 匹配结论：`{verdict: "yes"\|"no", reason}` |
| `dimensions` | object | 7 维得分结构（见下） |
| `missing_skills` | array | 缺失技能清单，**按图谱权重降序**（final_score 支持度，高在前） |
| `learning_path` | array | 学习路径（B3 契约，见 [learning-path-schema.md](learning-path-schema.md)） |
| `overall_advice` | str | 整体建议 |

## dimensions 单维结构（0-1 得分 + 命中/缺失）

```json
{
  "knowledge": {
    "score": 0.5,          // 0-1 覆盖率（命中数 / 该维技能总数）
    "gap_level": "missing | partial | sufficient",
    "summary": "一句话维度差距结论",
    "hit": ["已命中技能1", "已命中技能2"],
    "missing": ["缺失技能1"]
  }
}
```

7 个维度固定：`knowledge / skill / qualifications / preference / motivation / trait / self_concept`。
维度无图谱技能（如 preference 当前为 0 条）时 `score=0`、`gap_level=sufficient`（视为无要求），前端渲染"暂无数据"。

## missing_skills 单条

```json
{"skill": "Kafka", "dim": "skill", "importance": "high", "weight": 0.62}
```

`importance` 枚举：`high / medium / low`（LLM 复核标注，按技能名合并，查不到回退 medium）；
`weight` 为该技能在岗位图谱中的 `final_score`（JD 支持度，0~1），排序与展示均以此为准。
学习路径步骤顺序由 LLM 综合重要性与前置依赖给出（输入为该权重降序清单）。

## JSON Schema（摘要）

```json
{
  "type": "object",
  "required": ["role_name", "match", "dimensions", "missing_skills", "learning_path", "overall_advice"],
  "properties": {
    "dimensions": {
      "type": "object",
      "patternProperties": {
        "^(knowledge|skill|qualifications|preference|motivation|trait|self_concept)$": {
          "type": "object",
          "required": ["score", "gap_level", "summary", "hit", "missing"],
          "properties": {
            "score": {"type": "number", "minimum": 0, "maximum": 1},
            "gap_level": {"enum": ["missing", "partial", "sufficient"]}
          }
        }
      }
    },
    "missing_skills": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["skill", "dim", "importance"],
        "properties": {
          "skill": {"type": "string"},
          "dim": {"type": "string"},
          "importance": {"enum": ["high", "medium", "low"]}
        }
      }
    }
  }
}
```

## 真实样例

完整结构化输出见 [sample-gap-response.json](sample-gap-response.json)
（基于真实简历 faircv_000_后端开发工程师 + Neo4j 图谱 Java 开发工程师的 rank 结果；
LLM 部分为 mock 占位内容，接入真实模型后 summary/建议为语义文本，结构不变）。

