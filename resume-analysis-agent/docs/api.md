# HTTP API 说明（B1）— 前端联调

服务入口：[api_server.py](../api_server.py)（FastAPI，Swagger 自动文档见 `/docs`）。
所有业务逻辑复用 `src/tools/`，本文件只做请求/响应序列化。

## 启动

```powershell
# 推荐：使用项目指定 Python（pyw1，含 fastapi/uvicorn/markitdown/matplotlib）
C:\Users\Kianak901\anaconda3\envs\pyw1\python.exe api_server.py
# 或
uvicorn api_server:app --host 0.0.0.0 --port 8000 --reload
```

默认 `http://127.0.0.1:8000`。启动前确认 `.env`：

```dotenv
STORE_BACKEND=memory    # 本地联调开箱即用；连图谱用 neo4j（需 Neo4j 已启动 + NEO4J_PASSWORD）
LLM_PROVIDER=deepseek   # 切换键；extract/enhance/gap/modify 需要当前供应商的凭证
DEEPSEEK_API_KEY=...
DEEPSEEK_MODEL=...      # 留空用预设
DEEPSEEK_BASE_URL=...   # 留空用预设
```

## 路由一览

| 方法 | 路径 | 是否调 LLM | 说明 |
|---|---|---|---|
| GET | `/health` | 否 | 健康检查、数据源、岗位数、LLM 配置 |
| POST | `/upload` | 否 | 简历文件（PDF/DOCX/MD/TXT）→ Markdown 文本 |
| POST | `/extract` | 是 | 简历 → 7 维画像（含防幻觉校验） |
| POST | `/rank` | 否 | 关键词命中粗排 Top-N + 七维覆盖率 |
| POST | `/enhance` | 是 | 语义复核粗排结果 |
| POST | `/gap` | 是 | 差距分析：`analysis` + `markdown` + 结构化 `report` |
| POST | `/modify` | 是 | 简历修改建议 + 防造假校验 |
| POST | `/radar` | 否 | 七维雷达图 PNG（直接 `img src` 可用） |

## 示例请求 / 响应

### GET /health

```bash
curl http://127.0.0.1:8000/health
```

```json
{"status": "ok", "service": "resume-analysis-api", "store_backend": "neo4j",
 "roles_available": 75, "llm_configured": true}
```

### POST /upload

```bash
curl -F "file=@简历.pdf" http://127.0.0.1:8000/upload
```

```json
{"filename": "简历.pdf", "text": "## 个人信息\n- 姓名：张三\n...（Markdown 原文）"}
```

### POST /rank

```bash
curl -X POST http://127.0.0.1:8000/rank -H "Content-Type: application/json" \
  -d '{"resume_text": "</upload 返回的 text>", "topk": 5}'
```

响应（节选）：

```json
{
  "topk": 5,
  "count": 5,
  "results": [
    {
      "role_name": "Java开发工程师",
      "family_name": "软件研发",
      "domain_name": "新一代信息技术",
      "score": 0.3333,
      "hit_skills": 10,
      "total_skills": 30,
      "dimensions": {
        "knowledge": {"hit": ["后端"], "miss": ["微服务"], "coverage": 0.5,
                      "total": 2, "hit_count": 1, "miss_count": 1},
        "skill": {"hit": ["Java", "Spring Boot", "MySQL", "Redis"], "miss": [...],
                  "coverage": 0.3077, "total": 13, "hit_count": 4, "miss_count": 9}
      }
    }
  ]
}
```

完整示例：`results/_api_rank.json`。

### POST /extract

```bash
curl -X POST http://127.0.0.1:8000/extract -H "Content-Type: application/json" \
  -d '{"resume_text": "<text>", "position": "机器学习工程师"}'
```

响应：`{position, dimensions: {7 维数组}, truncations, stats, validation}`，
`validation.ok=false` 时前端应展示 `validation.violations` 提示用户核对。

### POST /enhance

```bash
curl -X POST http://127.0.0.1:8000/enhance -H "Content-Type: application/json" \
  -d '{"rank_result": {<rank 完整返回>}, "resume_text": "<text>", "topk": 5}'
```

响应：`{topk, results: [{role_name, score, hit_skills, total_skills, review_note, dimensions}]}`。

### POST /gap

```bash
curl -X POST http://127.0.0.1:8000/gap -H "Content-Type: application/json" \
  -d '{"role": {<rank/enhance 中单个 role>}, "resume_text": "<text>"}'
```

响应：`{role_name, analysis, markdown, report}`，其中 `report` 为结构化契约
（见 [gap-schema.md](gap-schema.md)），`markdown` 为可直接展示的报告文本。

### POST /modify

```bash
curl -X POST http://127.0.0.1:8000/modify -H "Content-Type: application/json" \
  -d '{"role": {<单个 role>}, "resume_text": "<text>"}'
```

响应：`{role_name, analysis, markdown, validation}`，`validation` 为防造假校验报告，
`valid=false` 且有 critical 违规时前端应提示用户先修正。

### POST /radar

```bash
curl -X POST http://127.0.0.1:8000/radar -H "Content-Type: application/json" \
  -d '{"role": {<单个 role>}, "role_name": "Java开发工程师"}' -o radar.png
```

直接返回 PNG（`Content-Type: image/png`），前端 `<img src="http://127.0.0.1:8000/radar">` 或 fetch 后 Blob 展示。

## 无 LLM 凭证时的联调

`extract/enhance/gap/modify` 依赖有效 LLM 凭证。密钥不可用时可用本地 mock
（Switch 模式，用 `CUSTOM` 配置块指向 mock 服务）：

```powershell
python tests/mock_llm_server.py                     # 终端 1：mock LLM（9009 端口）
$env:LLM_PROVIDER="custom"                          # 终端 2：API 指向 mock
$env:CUSTOM_API_KEY="mock"
$env:CUSTOM_BASE_URL="http://127.0.0.1:9009/v1"
$env:CUSTOM_MODEL="mock-model"
python api_server.py
```

mock 返回固定合法 JSON，用于验证链路与前端渲染；接入真实模型后替换 `.env` 即可。

