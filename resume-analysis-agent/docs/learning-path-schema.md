# 学习路径 schema（B3）

学习路径是 gap 响应的子契约，前端**按字段渲染卡片，无需解析 Markdown**。

## 字段（固定顺序与含义）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `step` | int | 是 | 步骤序号（从 1 开始） |
| `skill` | str | 是 | 学习目标技能/主题 |
| `importance` | str | 是 | `high` / `medium`（低优先级的缺失项不进路径） |
| `prerequisite` | str | 是 | 前置要求，无则 `"无"` |
| `resources` | array[str] | 是 | 推荐资源（课程/文档/开源项目/书籍），可为空数组 |
| `estimated_effort` | str | 是 | 预估投入，如 `"2-3 周"`，可为空字符串 |
| `why` | str | 是 | 为什么排在当前顺序（依赖关系/优先级依据），可为空字符串 |

## 排序规则

- 输入给 LLM 的缺失技能清单**已按图谱权重（`final_score` 支持度）降序预排序**；
- LLM 在生成 `learning_path` 时综合**重要性（权重高优先）+ 前置依赖（必须先学）**
  给出最终步骤顺序，可能因前置关系与输入清单顺序不同；
- `importance` 由 LLM 标注（`high` / `medium`），低优先级缺失项不进路径。

## 样例

```json
{
  "learning_path": [
    {
      "step": 1,
      "skill": "微服务",
      "importance": "high",
      "prerequisite": "Java 基础",
      "resources": ["Spring Cloud 官方文档", "微服务架构实战"],
      "estimated_effort": "2-3 周",
      "why": "岗位核心知识维度缺失，且为后续技术栈的前置"
    }
  ]
}
```

完整样例见 [sample-learning-path.json](sample-learning-path.json)。

## 前端渲染建议

- 卡片标题 = `skill`；右上角徽标 = `importance`（high 红色 / medium 橙色）。
- `prerequisite` 为空或 `"无"` 时不展示前置行。
- `resources` 为链接列表；`estimated_effort` 显示"预计投入"。
- `why` 作为卡片底部说明文字。

