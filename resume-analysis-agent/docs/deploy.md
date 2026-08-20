# 部署与运行说明（B4）

## 1. 依赖

```bash
pip install -e .                        # 项目本体（markitdown、matplotlib、numpy、dotenv、mcp 等）
pip install fastapi "uvicorn[standard]" python-multipart   # HTTP API（B1）
```

本机已验证环境：`C:\Users\Kianak901\anaconda3\envs\pyw1\python.exe`（含 mcp / fastapi / langchain_openai / markitdown）。

## 2. `.env` 配置

复制 `.env.example` 为 `.env` 并填写：

```dotenv
LLM_PROVIDER=deepseek          # 切换键：deepseek | iflytek | openai | custom

DEEPSEEK_API_KEY=              # extract/enhance/gap/modify 路由必需（对应供应商块）
DEEPSEEK_BASE_URL=             # 留空用预设 https://api.deepseek.com/v1
DEEPSEEK_MODEL=                # 如 deepseek-v4-flash

STORE_BACKEND=memory           # memory（开箱即用 6 个示例）| neo4j
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=
NEO4J_DATABASE=neo4j
```

LLM 配置采用**并列 Switch 模式**：每个供应商一个独立块 `{PROVIDER}_*`（`DEEPSEEK_*` / `IFLYTEK_*` /
`OPENAI_*` / `CUSTOM_*`），`LLM_PROVIDER` 切换，互不干扰；已废弃共享的
`LLM_API_KEY / LLM_MODEL / LLM_BASE_URL / LLM_EXTRA_BODY`。

> 历史（2026-08-11）：旧版共享变量优先/回落曾导致 DeepSeek 请求误走讯飞网关；现改为 Switch 模式后不再存在。
> 讯飞 key 曾欠费 403；旧讯飞配置已并入 `.env` 的 `IFLYTEK_*` 块（充值后把 `LLM_PROVIDER` 切回 iflytek 即可），`.env.bak` 已删除。

## 3. 数据源

- `STORE_BACKEND=memory`：内嵌 6 个示例岗位，开箱即用。
- `STORE_BACKEND=neo4j`：需 Neo4j 已启动（默认 `bolt://localhost:7687`）且配置 `NEO4J_PASSWORD`。
  实测当前图谱：134 个标准岗位，75 个有 `HAS_CORE_SKILL` 技能可参与匹配。

## 4. 启动

```powershell
# 终端 1：MCP（桌面 Agent 接入）
python mcp_server.py

# 终端 1：HTTP API（前端联调，Swagger 在 /docs）
python api_server.py                    # 或 uvicorn api_server:app --host 0.0.0.0 --port 8000

# 无 LLM 凭证时另开终端 2，API 指向本地 mock（见 docs/api.md）
python tests/mock_llm_server.py
```

## 5. 示例 curl 序列（端到端）

```bash
# 1) 健康检查
curl http://127.0.0.1:8000/health

# 2) 上传简历 → Markdown
curl -F "file=@简历.pdf" http://127.0.0.1:8000/upload -o upload.json

# 3) 提取 7 维画像（需 LLM）
curl -X POST http://127.0.0.1:8000/extract -H "Content-Type: application/json" \
  -d "{\"resume_text\": \"$(cat upload.json | jq -r .text)\", \"position\": \"后端开发工程师\"}" -o extract.json

# 4) 粗排
curl -X POST http://127.0.0.1:8000/rank -H "Content-Type: application/json" \
  -d "{\"resume_text\": \"$(cat upload.json | jq -r .text)\", \"topk\": 5}" -o rank.json

# 5) 取第 1 名做差距分析（需 LLM）
curl -X POST http://127.0.0.1:8000/gap -H "Content-Type: application/json" \
  -d "{\"role\": $(jq '.results[0]' rank.json), \"resume_text\": \"$(cat upload.json | jq -r .text)\"}" -o gap.json

# 6) 雷达图
curl -X POST http://127.0.0.1:8000/radar -H "Content-Type: application/json" \
  -d "{\"role\": $(jq '.results[0]' rank.json), \"role_name\": \"Java开发工程师\"}" -o radar.png
```

Windows PowerShell 无 `jq` 时，直接用 Python/前端代码调用（请求体示例见 [api.md](api.md)）。

## 6. 端到端 demo 产物

用真实简历 `samples/faircv_fivedim/000_后端开发工程师.json` 跑通的链路产物：

| 步骤 | 产物 | 说明 |
|---|---|---|
| upload | `results/_demo_upload.json` | PDF/DOCX/MD 转 Markdown（demo 直接传文本，等价于 upload 输出） |
| extract | `results/_demo_extract.json` | 7 维画像 + 防幻觉校验 |
| rank | `results/_demo_rank.json` | Top-5 + 七维覆盖率 |
| gap | `results/_demo_gap.json` | 结构化 report + markdown |
| radar | `results/_demo_radar.png` | 七维雷达图 |

> LLM 步骤使用真实 DeepSeek（`deepseek-v4-flash`）跑通；
> 无凭证时可用本地 mock（`tests/mock_llm_server.py`）离线复现同一链路。

