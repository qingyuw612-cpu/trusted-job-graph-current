# 真实联调启动清单

这个前端不是静态演示。要让所有页面都有真实数据，需要同时启动三个部分：

1. `qianduan/html-main2` 静态前端服务：默认 `8090`
2. `resume-analysis-agent` 简历分析 API：默认 `8000`
3. `trusted-job-graph-current` 岗位图谱 API + Neo4j：默认 `8010`

完成 Neo4j 连接配置后，可在项目根目录一键启动：

```powershell
.\resume-analysis-agent\.venv\Scripts\python.exe start_demo.py
```

## 1. 前端服务

```powershell
cd qianduan\html-main2
..\..\resume-analysis-agent\.venv\Scripts\python.exe -m http.server 8090 --bind 127.0.0.1
```

打开：

```text
http://127.0.0.1:8090/index.html
```

## 2. 简历分析服务

```powershell
cd resume-analysis-agent
.\.venv\Scripts\python.exe api_server.py
```

检查：

```text
http://127.0.0.1:8000/health
```

`/rank` 不需要大模型 Key；`/gap`、`/modify` 需要 Key。统一前端的 `resume-match.html` 已经提供了“模型设置”面板，用户可以输入自己的 DeepSeek Key，本次请求临时使用，不写入 `.env`。

如果仍想用 `.env` 固定配置，则复制：

```powershell
Copy-Item .env.example .env
```

然后编辑：

```dotenv
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=你的 DeepSeek Key
```

## 3. Neo4j 与岗位图谱服务

岗位图谱页面需要 Neo4j。推荐安装 **Neo4j Desktop**，也可以安装 Neo4j Community Server。

### 安装 Neo4j Desktop

1. 下载并安装 Neo4j Desktop。
2. 新建一个本地 DBMS，建议单独建空数据库用于展示包。
3. 设置用户名 `neo4j`，记住密码。
4. 启动数据库。
5. 确认 Bolt 地址是 `bolt://127.0.0.1:7687`，HTTP 地址是 `http://127.0.0.1:7474`。

### 创建连接配置

```powershell
cd trusted-job-graph-current
Copy-Item config\neo4j_connection.example.json config\neo4j_connection.json
```

编辑 `config\neo4j_connection.json`：

```json
{
  "http_uri": "http://127.0.0.1:7474",
  "bolt_uri": "bolt://127.0.0.1:7687",
  "database": "neo4j",
  "username": "neo4j",
  "password": "你的 Neo4j 密码"
}
```

`instance_dir`、`import_dir`、`cypher_shell`、`java_home` 可先保留示例值，展示包导入不需要它们。

### 导入展示图谱

根目录已有：

```text
可信岗位图谱Agent\output\team_handoff\display_graph.json
```

导入到空数据库：

```powershell
python display_graph_handoff.py import `
  --package "..\可信岗位图谱Agent\output\team_handoff\display_graph.json" `
  --neo4j-config config\neo4j_connection.json
```

验证：

```powershell
python display_graph_handoff.py verify `
  --neo4j-config config\neo4j_connection.json
```

启动图谱服务：

```powershell
python display_graph_handoff.py serve `
  --neo4j-config config\neo4j_connection.json
```

检查：

```text
http://127.0.0.1:8010/api/health
```

然后打开统一前端图谱页：

```text
http://127.0.0.1:8090/panorama.html
```

也可以打开原始负责人页面核对效果：

```text
http://127.0.0.1:8010/
```

统一前端的图谱页同样调用 `8010/api/...`，如果原始页面能看到图谱而统一页看不到，优先检查浏览器缓存，使用 `Ctrl + F5` 强制刷新。

## 一键检查

前端目录提供了检查脚本：

```powershell
cd qianduan\html-main2
.\check-services.ps1
```

它会检查：

- `neo4j_connection.json` 是否存在
- `display_graph.json` 是否存在
- `8010` 图谱 API 是否在线
- `8000` 简历 API 是否在线
- `8090` 前端是否在线

## 常见问题

- `缺少 DEEPSEEK_API_KEY`：只影响差距分析/修改建议。可以在 `resume-match.html` 的“模型设置”里输入 Key（仅用于当前请求），或写入 `resume-analysis-agent\.env`。
- 图谱页空白：通常是 Neo4j 没启动、展示包没导入、`config\neo4j_connection.json` 密码错误，或 `8010` 图谱服务没启动。
- 技能证据详情为空：展示包不包含原始 JD 和证据原文，这是数据边界限制，不是前端错误。
