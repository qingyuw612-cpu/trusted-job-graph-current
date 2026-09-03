# 简历人岗匹配 MCP 工具套件

基于关键词命中的简历-岗位匹配分析 **MCP Server**（产品名：简历岗位匹配分析；MCP 注册名：resume-analysis）：`PDF/DOCX → Markdown → 关键词命中粗排 → Agent 语义复核 → 差距分析 → 简历修改建议 → 七维雷达图`。

双模式：**MCP 模式**的语义复核 / 差距分析 / 简历修改由调用方 Agent 用自己的大模型完成，服务器不调用外部 LLM API；**CLI 模式**的 `enhance` / `analyze` / `modify` / `extract-resume` 通过统一 LLM 适配器调用（默认 DeepSeek，可切讯飞星火 / OpenAI 兼容服务）。

支持 Claude Desktop、Codex、Cursor、Continue 等任意兼容 MCP 的 Agent 平台即插即用；无 MCP 环境时也可用 CLI 或直接调用 `src/tools/` 函数。

## 维度口径

**核心口径为 7 维**：`knowledge`（知识）/ `skill`（技术）/ `qualifications`（任职条件）/ `preference`（招聘偏好）/ `motivation`（动机）/ `trait`（特质）/ `self_concept`（自我概念），严格对齐 Neo4j 图谱 `NormalizedSkill.category`（见 `src/core/dimensions.py` 的 `CATEGORY_TO_DIM` / `DIM_TO_CATEGORY`）。

挑战杯大纲的**"五分类"（知识/技术/动机/特质/自我概念）仅为原始数据/汇报口径**（如 `samples/faircv_fivedim/`、`jds/` 中的旧 `five_dim` 字段），不参与核心匹配。对外材料如需五分类，使用 `project_to_five_dim()` 做 7→5 投影（任职条件归入知识、招聘偏好归入动机）。

## 快速开始

```bash
# 1. 安装依赖
pip install -e .

# 2. 复制 .env.example 为 .env 并填写 DEEPSEEK_API_KEY

# 3. 启动 MCP Server（stdio 默认，供桌面 Agent 接入）
python mcp_server.py

# SSE 传输（可选）
python mcp_server.py --transport sse
```

## MCP 工具

| 工具 | 输入 | 输出 | 说明 |
|------|------|------|------|
| `rank_resume` | 简历文本 (str) + topk (int) | Top-N Role + 七维覆盖率 JSON | 纯关键词命中粗排 |
| `prepare_enhance` | 排名 JSON + 简历原文 + topk | 复核提示包（prompt + rank_data + schema） | Agent 用自己的模型复核 |
| `apply_enhance_review` | rank JSON + 复核 JSON | 合并后的 JSON（含 review_note） | 纯逻辑合并 + 重算覆盖率/得分 |
| `visualize_radar` | 单 Role JSON + 岗位名 | PNG 图片（对话中直接渲染） | 七维雷达图 |
| `prepare_gap` | 单 Role JSON + 简历原文 | 差距分析提示包 | Agent 用自己的模型输出 Markdown |
| `prepare_resume_edit` | 单 Role JSON + 简历原文 | 简历修改提示包（含真实性红线） | Agent 用自己的模型输出修改建议 |
| `validate_resume_edit` | 单 Role JSON + 简历原文 + 修改建议 JSON | 防造假校验报告（valid/violations/stats） | 纯逻辑校验技能地基、指标地基、AI 味词汇 |
| `prepare_resume_extract` | 简历原文 + 目标岗位（可选） | 7 维画像提取提示包（prompt + schema） | Agent 用自己的模型输出画像 |
| `apply_resume_extract` | 简历原文 + 提取 JSON | 规范化 7 维画像 + 防幻觉校验（超长自动截断标注） | 纯逻辑，不调用 LLM |

两个静态资源供 Agent 参考：`dimensions://seven`（七维定义）、`dimensions://category-map`（Neo4j category → 七维 key 映射）。

## Agent 接入（mcp.json）

将 `mcp.json.example` 复制到对应平台的 MCP 配置（路径按实际安装目录调整）：

- **Claude Desktop**：`claude_desktop_config.json` 的 `mcpServers`
- **Cursor**：项目 `.cursor/mcp.json`
- **Continue**：`~/.continue/config.json` 的 `mcpServers`
- **Codex / 其他 CLI**：`codex mcp add` 指向 `python mcp_server.py`

> 注意：`command` 必须指向**装有 mcp SDK 的 Python**（如 conda 环境的绝对路径），
> 不能是系统默认 `python`（若其未安装 mcp）。`mcp.json.example` 已给出本机示例。

### Codex 接入（config.toml）

编辑 `C:\Users\Kianak901\.codex\config.toml`，在 `[mcp_servers]` 段后追加：

```toml
[mcp_servers.resume-analysis]
command = 'C:\Users\Kianak901\anaconda3\envs\pyw1\python.exe'
args = ['D:\个人资料\26暑假科研项目\小挑\简历提取分析agent\mcp_server.py']
startup_timeout_sec = 30

[mcp_servers.resume-analysis.env]
STORE_BACKEND = "memory"
```

保存后**重启 Codex**（MCP server 在启动时加载），新会话即可使用
`rank_resume` / `prepare_enhance` / `apply_enhance_review` / `prepare_gap` / `prepare_resume_edit` / `validate_resume_edit` / `prepare_resume_extract` / `apply_resume_extract` / `visualize_radar` 九个工具。

或使用 CLI 命令添加（效果相同）：

```powershell
codex mcp add resume-analysis -- C:\Users\Kianak901\anaconda3\envs\pyw1\python.exe "D:\个人资料\26暑假科研项目\小挑\简历提取分析agent\mcp_server.py"
```

## CLI 用法（非 MCP 场景）

```bash
python src/main.py rank -r 简历.pdf --topk 10
python src/main.py enhance -r rank_result.json --resume 简历.pdf --topk 20
python src/main.py enhance -r rank_result.json --resume 简历.pdf --topk 20 --analyze   # 复核后自动对第 1 名做差距分析
python src/main.py analyze -r role.json --resume 简历.pdf
python src/main.py modify -r role.json --resume 简历.pdf   # 针对目标岗位的简历修改建议
python src/main.py extract-resume -i resumes/ -o resume_profiles.json --workers 4   # 简历 → 7维画像（LLM 批量提取，4 线程并发）
python src/main.py extract-resume -i samples/faircv_sample_100.json -o faircv_profiles.json --provider iflytek
python src/main.py --store neo4j rank -r 简历.pdf
```

## HTTP API（FastAPI，前端联调）

服务入口 `api_server.py`，Swagger 自动文档：`http://127.0.0.1:8000/docs`。

```powershell
# 启动（推荐用项目环境 pyw1，已装 fastapi/uvicorn）
C:\Users\Kianak901\anaconda3\envs\pyw1\python.exe api_server.py
# 或
uvicorn api_server:app --host 0.0.0.0 --port 8000 --reload
```

路由一览（`/extract /enhance /gap /modify` 需要 `.env` 中当前 `LLM_PROVIDER` 的凭证）：

| 方法 | 路径 | 调 LLM | 说明 |
|---|---|---|---|
| GET | `/health` | 否 | 健康检查 / 数据源 / 岗位数 / LLM 配置 |
| POST | `/upload` | 否 | 简历文件（PDF/DOCX/MD/TXT）→ Markdown 文本 |
| POST | `/extract` | 是 | 简历 → 7 维画像（防幻觉校验） |
| POST | `/rank` | 否 | 关键词命中粗排 Top-N + 七维覆盖率 |
| POST | `/enhance` | 是 | 语义复核粗排结果 |
| POST | `/gap` | 是 | 差距分析（`analysis` + `markdown` + 结构化 `report`） |
| POST | `/modify` | 是 | 简历修改建议 + 防造假校验 |
| POST | `/radar` | 否 | 七维雷达图 PNG（可直接 `img src`） |

最小调用示例：

```bash
# 1. 健康检查
curl http://127.0.0.1:8000/health

# 2. 上传简历，拿到返回的 text 字段（后续接口复用）
curl -F "file=@简历.pdf" http://127.0.0.1:8000/upload

# 3. 粗排（resume_text 用上一步返回的 text）
curl -X POST http://127.0.0.1:8000/rank \
  -H "Content-Type: application/json" \
  -d '{"resume_text": "简历Markdown原文", "topk": 10}'
```

完整请求/响应示例见 [docs/api.md](docs/api.md)。

## LLM 供应商切换

CLI 通过 `src/utils/llm.py` 的统一适配器调用模型（OpenAI 兼容协议），支持多供应商。
配置采用**并列 Switch 模式**：每个供应商一个独立配置块 `{PROVIDER}_*`，`LLM_PROVIDER` 是切换键，互不干扰；
不使用共享的 `LLM_API_KEY / LLM_MODEL / LLM_BASE_URL / LLM_EXTRA_BODY`。

```ini
LLM_PROVIDER=deepseek          # 切换键：deepseek | iflytek | openai | custom

DEEPSEEK_API_KEY=...
DEEPSEEK_BASE_URL=             # 留空用预设 https://api.deepseek.com/v1
DEEPSEEK_MODEL=                # 留空用预设 deepseek-chat

IFLYTEK_API_KEY=...
IFLYTEK_BASE_URL=              # 留空用预设 https://spark-api-open.xf-yun.com/v1
IFLYTEK_MODEL=                 # 留空用预设 4.0Ultra
IFLYTEK_EXTRA_BODY=            # 推理模型参数，如 {"thinking":{"type":"enabled"}}

CUSTOM_API_KEY=...
CUSTOM_BASE_URL=               # 必填
CUSTOM_MODEL=                  # 必填
```

- 预设端点：DeepSeek `api.deepseek.com/v1`、讯飞星火 `spark-api-open.xf-yun.com/v1`、OpenAI `api.openai.com/v1`；
- 切换供应商只需改 `LLM_PROVIDER`；`--provider` 临时切换（如 A/B 对比 DeepSeek vs 讯飞）同样只读对应配置块；
- 讯飞推理模型 `spark-x` 需在 `IFLYTEK_EXTRA_BODY` 传 `{"thinking":{"type":"enabled"}}`。

## 目录结构

```
resume-analysis-agent/
├── mcp_server.py            # MCP 入口（唯一平台边界）
├── AGENTS.md                # Agent 操作指南（工作流 + 工具表）
├── mcp.json.example         # 各平台配置模板
├── src/
│   ├── main.py              # CLI 入口（rank / enhance / analyze / modify / extract-resume）
│   ├── core/                # 纯逻辑层（零外部依赖，可单测）
│   │   ├── dimensions.py    #   七维定义 + category 双向映射 + 7→5 投影 + 权重
│   │   ├── matching.py      #   归一化 + 命中搜索
│   │   ├── ranking.py       #   覆盖率粗排 + 少条目惩罚 + IDF
│   │   └── review.py        #   复核结果合并（重算覆盖率/得分）
│   ├── store/               # 数据抽象层（依赖倒置，核心不 import neo4j）
│   │   ├── interface.py     #   RoleStore 抽象接口
│   │   ├── memory_store.py  #   内存示例数据（默认，开箱即用）
│   │   └── neo4j_store.py   #   Neo4j 实现
│   ├── tools/               # 工具纯函数（rank/enhance/analyze/modify/visualize/extract）
│   ├── prompts/             # LLM 提示词模板
│   └── utils/               # LLM / markitdown 封装
├── tests/                   # pytest 单测（core / store / tools）
└── archive/legacy/          # 旧 Streamlit/LangGraph 代码（已隔离，不参与运行）
```

## 数据源切换

`STORE_BACKEND` 环境变量选择数据实现（`src/store/interface.py` 依赖倒置）：

- `memory`（默认）：内嵌 6 个示例 Role，适合演示 / CI / 测试
- `neo4j`：加载图谱全部 Role（134 个），需配置 `NEO4J_PASSWORD`

## 评分公式

```
Role 得分 = (Σ 命中技能 final_score / Σ 核心技能 final_score) × min(1, 核心技能数 / 10)
```

- `final_score`：图谱 `HAS_CORE_SKILL` 边权重（JD 支持度），加权覆盖率语义为
  "候选人覆盖了该岗位 JD 需求质量的百分比"；全部权重为 0 时回退为纯命中率。
- 少条目惩罚：核心技能 < 10 的岗位按 n/10 打折，防止稀疏岗位无脑排前。
- IDF 跨岗位重加权默认关闭，可经 `use_idf` 开关消融对比。
- 语义复核（`enhance` / `apply_enhance_review`）合并后按同一加权口径重算分数，
  rank 结果中的 `skill_weights` 供复核合并查权重，粗排与复核分数口径一致。
- 任职条件的学历要求按**等级语义判定**（如"本科"命中"本科及以上学历"、
  "本科"不命中"硕士及以上学历"），而非纯子串匹配。
- 匹配方式：技能名与简历原文统一去空白/标点、全角转半角、转小写后做归一化子串包含判断。

## 测试

```bash
python -m pytest tests -q
```

核心层（`src/core/`）为零外部依赖纯函数，测试不触网、不调 LLM。

