# 图谱 API 契约（A2）— 简历匹配 agent ↔ 图谱（二作）

> 状态：**草稿 v0.1（待二作确认）**。本文件定义简历匹配 agent 从 Neo4j 图谱读取
> Role 画像时的字段契约、必备/加分技能区分、领域/岗位族口径、JD 数量口径，
> 以及 45/134 覆盖问题的处理方案。

## 1. 数据现状（results/neo4j_roles.json 实测）

- 图谱 `Role` 共 **134** 个；交接 dump（`results/neo4j_roles.json`）中**可匹配 45 个**（有 `HAS_CORE_SKILL` 技能且 `jd_count > 0`）。
- **2026-08-11 实时图谱实测**：`/health` 返回 **75 个**有 `HAS_CORE_SKILL` 的岗位可参与匹配（比旧 dump 多 30 个，说明图谱在持续补充），前端空匹配提示中的数量应以实时 `/health` 为准。
- 45 个可匹配岗位按岗位族分布：

| 岗位族（dump 口径） | 数量 | 岗位 |
|---|---|---|
| 软件研发 | 10 | .NET开发工程师、Android开发工程师、C++开发工程师、Go开发工程师、Java开发工程师、Python开发工程师、UE4开发工程师、Unity3D开发工程师、前端开发工程师、游戏开发工程师 |
| 数据技术 | 5 | BI工程师、ETL开发工程师、数据分析师、数据建模工程师、数据采集工程师 |
| 芯片与半导体 | 5 | EDA工程师、IC设计工程师、半导体工程师、芯片架构工程师、芯片测试工程师 |
| 硬件与嵌入式 | 8 | PCB工程师、光电子工程师、嵌入式硬件开发工程师、嵌入式软件开发工程师、电气工程师、电源开发工程师、硬件工程师、自动控制工程师 |
| 产品与设计 | 4 | 产品经理、平台产品经理、数据产品经理、需求工程师 |
| AI与算法 | 5 | 机器学习工程师、深度学习工程师、算法工程师、自然语言处理工程师、计算机视觉工程师 |
| 测试与质量 | 4 | 测试开发工程师、硬件测试工程师、自动化测试工程师、软件测试工程师 |
| 智能系统与低空技术 | 2 | 无人机工程师、智能驾驶工程师 |
| 网络与安全 | 1 | 网络工程师 |
| 运维与基础设施 | 1 | 运维工程师 |

- 技能总量 978 条，均有 `rank` 与 `weight`（图谱 `final_score`）。
- 七维类别覆盖：技术 532 / 特质 187 / 动机 88 / 任职条件 84 / 知识 50 / 自我概念 37；**招聘偏好（preference）为 0 条**，前端该维度应渲染为"暂无数据/0"，不能当作异常。

## 2. role 画像 schema（对外统一契约）

图谱原始字段 → agent 标准化后的 Role 画像：

```json
{
  "role_name": "Java开发工程师",
  "role_id": "",
  "family_name": "软件研发",
  "family_id": "",
  "domain_name": "新一代信息技术",
  "jd_count": 80,
  "skills": [
    {
      "name": "Java",
      "category": "技术",
      "dim": "skill",
      "weight": 0.905,
      "rank": 1,
      "evidence_count": 0,
      "skill_type": "core"
    }
  ]
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `role_name` | str | 是 | 标准岗位名（图谱 `Role.name` 对外统一用 `role_name`） |
| `role_id` | str | 否 | 图谱 `Role.role_id`（当前 dump 未导出，待二作补） |
| `family_name` | str | 是 | 岗位族名（dump 为"软件研发"等；与 `it_role_taxonomy.json` v2.0.0 的 family 名不一致，见 §5） |
| `family_id` | str | 否 | 岗位族 ID（taxonomy `family_id`，待对齐） |
| `domain_name` | str | 是 | 领域名（当前 dump 导出为损坏字符 `????`，待二作修复或 agent 反查 taxonomy） |
| `jd_count` | int | 是 | 关联 JD 数量 = `(:JD)-[:INSTANCE_OF]->(:Role)` 去重计数（dump 实测 80–183） |
| `skills[]` | list | 是 | 核心技能列表（按 `rank` 升序） |
| `skills[].name` | str | 是 | 归一化技能名（`NormalizedSkill.canonical_name`） |
| `skills[].category` | str | 是 | 七维类别：知识/技术/任职条件/招聘偏好/动机/特质/自我概念 |
| `skills[].dim` | str | 是 | `CATEGORY_TO_DIM` 投影后的七维 key（knowledge/skill/qualifications/preference/motivation/trait/self_concept） |
| `skills[].weight` | float | 是 | 图谱关系强度 `HAS_CORE_SKILL.final_score`（0–1） |
| `skills[].rank` | int | 是 | 图谱内技能排名（1 = 最核心） |
| `skills[].evidence_count` | int | 否 | 支持证据数 `edge.verified_jd_count`（当前 dump 未导出，待补） |
| `skills[].skill_type` | str | 是 | `core`（必备）/ `bonus`（加分），规则见 §3 |

## 3. 必备/加分技能区分（draft 规则，待二作/产品确认）

```text
core  = rank ≤ ceil(total_skills × 0.6)  或  category == "任职条件"
bonus = 其余
```

- 理由：图谱 `rank` 已是"该技能对该岗位的核心程度"排序；取前 60% 分位作为必备技能，硬性门槛（学历/专业等任职条件）无条件进必备。
- 该阈值可调；后续 C1 三维打分增强可改为按 `weight` 分段（如 ≥0.4 必备）。

## 4. JD 数量口径

- `jd_count` 固定按 `(:JD)-[:INSTANCE_OF]->(:Role)` 计数（agent 侧现有实现），与图谱 `Role.document_count` 含义不同：
  - `document_count` = 该岗位被多少 JD 文本引用（图谱侧口径）；
  - `jd_count` = 已建立 `INSTANCE_OF` 关系的 JD 数（匹配侧口径）。
- 二作确认用哪个作为对外口径；未确认前匹配侧沿用 `jd_count`。

## 5. 领域/岗位族对齐问题（待二作确认）

1. **taxonomy 版本不一致**：`it_role_taxonomy.json`（v2.0.0，55 个岗位）的 family 名（如"后端与服务端开发"）与 dump 的 family 名（如"软件研发"）不同；图谱 134 个岗位对应的 taxonomy 版本与 family 字段来源待确认。
2. **domain_name 损坏**：当前 dump 的 `domain_name` 全部为 `????`（导出时编码损坏），需二作重导，或由匹配侧按 taxonomy 反查（45 个岗位大多能在 v2.0.0 taxonomy 中找到，无法覆盖的给出空值提示）。

## 6. 45/134（实时 75/134）覆盖与前端空匹配提示方案

- 可匹配范围 = §1 列表中的岗位（dump 45 个；实时图谱 75 个）。其余岗位因缺少 `HAS_CORE_SKILL` 技能或未导出，当前**无法参与匹配**。
- 前端处理：
  1. `rank_resume` 返回 `count = 0` 或用户搜索岗位不在可匹配集合内时，展示：
     > "该岗位暂无图谱技能数据。当前可匹配岗位 {N}/134（N 取 /health 的 roles_available），其余待图谱补充 HAS_CORE_SKILL 后重新匹配。"
  2. 提供 taxonomy 建议：按 `it_role_taxonomy.json` 的 `aliases` 提示相近岗位名（如搜索"后端"提示 Java/C++/Go/Python/.NET 开发工程师）。
  3. 七维中 `preference` 无图谱数据时渲染为"暂无数据"，不参与覆盖率分母。

## 7. 待二作确认清单

- [ ] `Role` 节点是否已有 `family_name` / `domain_name` 属性？若无，对外契约改为由匹配侧从 taxonomy 反查。
- [ ] `domain_name` 损坏问题：重导 dump 或确认反查方案。
- [ ] `role_id`、`evidence_count`（`verified_jd_count`）是否纳入交接数据。
- [ ] `jd_count` 与 `document_count` 的对外口径。
- [ ] taxonomy 版本：以哪个为准（当前 dump 45 岗位 vs v2.0.0 55 岗位 vs 图谱 134 岗位）。
- [ ] 必备/加分阈值：60% 分位规则是否可接受。
- [ ] `HAS_CORE_SKILL.final_score` 语义确认：当前匹配侧按"JD 支持度"解释并直接作为
      加权权重（数值形态为 0~1 的出现比例）；若实际是专家重要性或归一化得分，请说明口径。

