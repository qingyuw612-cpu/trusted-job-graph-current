# 简历岗位匹配分析 MCP Agent — 操作指南

本仓库提供一个标准 MCP Server（`mcp_server.py`，工具名：简历岗位匹配分析），用于简历职位匹配分析：
关键词命中粗排 → Agent 语义复核 → 单岗位差距分析 → 简历修改建议 → 七维雷达图。

## 维度口径（先定，勿摇摆）

- **核心口径为 7 维**：`knowledge`（知识）/ `skill`（技术）/ `qualifications`（任职条件）/ `preference`（招聘偏好）/ `motivation`（动机）/ `trait`（特质）/ `self_concept`（自我概念），严格对齐 Neo4j 图谱 `NormalizedSkill.category`。
- 大纲"五分类"（知识/技术/动机/特质/自我概念）**仅为原始数据/汇报口径**，不参与核心匹配；对外材料如需五分类，用 `project_to_five_dim()` 做 7→5 投影。
- 所有提示词、画像 schema、雷达图均为 7 维；不要回退到五/六维口径。

## 双模式说明

- **MCP 模式（Agent 调用）**：服务器只做纯逻辑（粗排、提示包准备、复核结果合并、雷达图）。
  语义复核 / 差距分析 / 简历修改等 LLM 推理，由调用方 Agent 用自己的大模型完成，
  服务器不调用任何外部 LLM API（MCP 模式无需 LLM 凭证）。
- **CLI 模式（命令行）**：`enhance` / `analyze` / `modify` / `extract-resume` 子命令
  通过统一 LLM 适配器（`src/utils/llm.py`）调用，默认 DeepSeek，可切讯飞星火 / OpenAI 兼容服务（读 `.env`）。

## 工作流（MCP / Agent，推荐顺序）

1. **读取简历**：用户给出 PDF/DOCX/MD/TXT 路径时，先用 `markitdown`（或直接读文本）把简历转为 Markdown 原文。
2. **粗排**：调用 `rank_resume(resume_text, topk)` 得到 Top-N Role 及七维覆盖率 JSON。
3. **展示**：向用户展示 Top-N，格式建议：
   ```
   #1 Java开发工程师 33.3%（命中 10/30 技能）
   #2 Android开发工程师 25.0%
   ...
   ```
4. **语义复核**（可选）：调 `prepare_enhance(rank_json, resume_text, topk)` 拿提示包，
   用自己的模型按提示复核（修正误判的命中/缺失），再调 `apply_enhance_review(rank_json, review_json)`
   合并规范化，得到带 review_note 的结果。
5. **雷达图**：取复核后或粗排的 role JSON，调 `visualize_radar(role_json, role_name)`，返回 PNG 直接渲染。
6. **差距分析**：调 `prepare_gap(role_json, resume_text)` 拿提示包，用自己的模型输出
   Markdown 报告（匹配结论 / 各维分析 / 总体建议 / 学习路径；
   缺失技能清单按图谱权重降序预排序后交给模型，学习路径顺序由模型综合重要性与前置依赖给出）。
7. **简历修改**：调 `prepare_resume_edit(role_json, resume_text)` 拿提示包，用自己的模型输出
   针对性修改建议（遵守真实性红线，不重写全文）；建议产出后再调
   `validate_resume_edit(role_json, resume_text, edit_json)` 做防造假校验
   （技能地基 / 指标地基 / AI 味词汇，纯逻辑），有 critical 问题时先修正再给用户。
8. **简历画像提取**（可选）：CLI 用 `extract-resume` 把简历批量提取为 7 维画像 JSON
   （`knowledge/skill/qualifications/preference/motivation/trait/self_concept`），
   与岗位画像同 schema，作为后续维度对维度匹配与候选人雷达图的标准化输入；
   MCP/Agent 模式调 `prepare_resume_extract` 拿提示包，用自己的模型输出画像后
   再调 `apply_resume_extract(resume_text, extract_json)` 规范化 + 防幻觉校验
   （超长条目自动截断并记录 truncations）。

## 工具清单（MCP）

| 工具 | 输入 | 说明 |
|------|------|------|
| `rank_resume` | 简历文本 + topk | 纯关键词命中粗排，返回 Top-N + 七维覆盖率 |
| `prepare_enhance` | rank JSON + 简历文本 + topk | 返回语义复核提示包（prompt + rank_data + schema） |
| `apply_enhance_review` | rank JSON + 复核 JSON | 合并复核结果，按 skill_weights 重算加权得分（纯逻辑） |
| `prepare_gap` | 单 role JSON + 简历文本 | 返回差距分析提示包 |
| `prepare_resume_edit` | 单 role JSON + 简历文本 | 返回简历修改提示包（含真实性红线） |
| `validate_resume_edit` | 单 role JSON + 简历文本 + 修改建议 JSON | 防造假校验报告（valid/violations/stats） |
| `prepare_resume_extract` | 简历文本 + 目标岗位（可选） | 7 维画像提取提示包（prompt + schema） |
| `apply_resume_extract` | 简历文本 + 提取 JSON | 规范化 7 维画像 + 防幻觉校验（超长自动截断标注） |
| `visualize_radar` | 单 role JSON + 岗位名 | 七维雷达图 PNG |

## 资源

| 资源 | 说明 |
|------|------|
| `dimensions://seven` | 七维画像定义（knowledge/skill/qualifications/preference/motivation/trait/self_concept） |
| `dimensions://category-map` | Neo4j category → 七维 key 映射 |

## CLI 用法（命令行模式，调 DeepSeek API）

```bash
python src/main.py rank -r 简历.pdf --topk 20
python src/main.py enhance -r rank.json --resume 简历.pdf --topk 20
python src/main.py analyze -r role.json --resume 简历.pdf
python src/main.py modify -r role.json --resume 简历.pdf
python src/main.py extract-resume -i resumes/ -o resume_profiles.json
python src/main.py extract-resume -i samples/faircv_sample_100.json --provider iflytek --workers 4
```

## 数据源切换

- 默认 `STORE_BACKEND=memory`：内嵌 6 个示例 Role，开箱即用。
- 设为 `STORE_BACKEND=neo4j` 并从 `.env` 提供 `NEO4J_PASSWORD`：加载图谱全部 134 个 Role。

## 环境变量

复制 `.env.example` 为 `.env` 并填写：

```
LLM_PROVIDER=deepseek          # 切换键：deepseek | iflytek | openai | custom

# DeepSeek 配置块
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=             # 留空用预设 https://api.deepseek.com/v1
DEEPSEEK_MODEL=                # 留空用预设 deepseek-chat

# 讯飞配置块（切到 iflytek 时用 IFLYTEK_*）
IFLYTEK_API_KEY=
IFLYTEK_BASE_URL=
IFLYTEK_MODEL=

# 自定义 OpenAI 兼容服务（切到 custom 时用 CUSTOM_*，base_url/model 必填）
CUSTOM_API_KEY=
CUSTOM_BASE_URL=
CUSTOM_MODEL=

STORE_BACKEND=memory          # memory | neo4j
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=
NEO4J_DATABASE=neo4j
```

LLM 配置为**并列 Switch 模式**：每个供应商一个独立块 `{PROVIDER}_*`，`LLM_PROVIDER` 切换；
不再使用共享的 `LLM_API_KEY / LLM_MODEL / LLM_BASE_URL`。

## 本地运行

```bash
pip install -e .
python mcp_server.py            # stdio（Claude Desktop / Codex / Cursor 本地接入）
python mcp_server.py --transport sse
```

## 注意事项

- `rank_resume` 要求简历为纯文本/Markdown；PDF/DOCX 请先用 markitdown 转换（见 `src/utils/text.py`）。
- MCP 模式不依赖 LLM API Key；CLI 的 `enhance` / `analyze` / `modify` / `extract-resume` 依赖。
- 代码分层：`mcp_server.py`（平台边界）→ `src/tools/`（工具实现）→ `src/core/`（纯逻辑，零外部依赖）+ `src/store/`（数据抽象）。

