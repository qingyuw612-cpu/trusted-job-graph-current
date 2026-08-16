# Docker 部署与评审说明

## 一键评审（推荐）

仓库默认路径不依赖 Neo4j、模型权重、外部 API 或私有招聘数据。它使用
`demo_data/review_demo.json` 中人工编写的脱敏演示数据，可直接复现三项功能。

```bash
docker compose up --build
```

可选地先复制环境变量模板并按需修改端口或基础镜像：

```powershell
Copy-Item .env.example .env
```

服务健康后打开 <http://localhost:8080/>。统一入口中的导航可访问：

- 岗位能力全景图：<http://localhost:8080/>
- 新岗位发现：<http://localhost:8080/new-roles>
- 能力变化：<http://localhost:8080/ability-changes>
- 健康检查：<http://localhost:8080/healthz>

停止服务：

```bash
docker compose down
```

## 本机全量 Neo4j 模式

全量模式不会把数据库、密码或演化产物复制进镜像。它将本机已有连接配置和分析结果
以只读方式挂载，并通过 `host.docker.internal` 访问本机 Neo4j：

1. 在 Neo4j Desktop 中启动目标 DBMS，确认 <http://localhost:7474> 可访问。
2. 使用基础 Compose 文件和全量覆盖文件共同启动：

```powershell
$env:BASE_IMAGE = 'mcr.microsoft.com/devcontainers/python:1-3.12-bookworm' # Docker Hub 不可用时
docker compose -f compose.yaml -f compose.full.yaml up --build -d
```

全量模式健康接口的 `backend` 应为 `neo4j`，而不是 `sanitized_demo`：

```powershell
Invoke-RestMethod http://localhost:8080/healthz
```

单独执行 `docker compose up` 会回到安全的脱敏演示模式。全量模式依赖本机被忽略的
`config/neo4j_connection.json` 和 `output/role_evolution_workbench_v2/`，不得将这两处
真实配置与产物提交进版本库。

如 8080 端口已占用，可先设置 `APP_PORT`，例如 `APP_PORT=18080 docker compose up --build`。

若当前网络无法访问 Docker Hub 的 `auth.docker.io`，可临时改用微软官方 Python
开发容器基础镜像，不需要修改仓库文件：

```powershell
$env:BASE_IMAGE = 'mcr.microsoft.com/devcontainers/python:1-3.12-bookworm'
docker compose up --build
```

网络恢复后删除该环境变量即可回到体积更小的 `python:3.12-slim`。

## 演示数据与安全边界

默认数据只含虚构企业名、聚合数字和人工编写的短证据句，不来自原始 JD，也不含个人信息、真实凭据或私有配置。修改演示数据后可用以下命令验证 JSON 和接口：

```bash
python -m json.tool demo_data/review_demo.json > /dev/null
python -m pytest
```

不要把以下内容加入镜像或版本库：完整 Neo4j dump、`config/neo4j_connection.json`、
`.env`、API Key、模型目录、原始 JD、运行日志和 `output/` 产物；忽略规则与 Docker
构建上下文均已排除这些路径。

## 可选 Neo4j 服务与恢复

默认评审功能完整，不需要启动 Neo4j。若需要检查已有 Neo4j 导入流程，可启用可选 profile：

```bash
# 先换成长随机口令；不要提交这个值
export NEO4J_AUTH='neo4j/replace-with-a-long-random-password'
docker compose -f compose.yaml -f compose.neo4j.yaml --profile neo4j up -d neo4j
```

Neo4j 端口只绑定本机 `127.0.0.1`，数据保存到命名卷 `neo4j_data`。浏览器地址为
<http://localhost:7474>，Bolt 地址为 `bolt://localhost:7687`。

推荐恢复方式是使用项目产生的脱敏 CSV/Cypher 分阶段导入，而不是提交完整 dump：

1. 将审核后的导入文件放在本机 `neo4j/import/`（该目录被忽略）。
2. 启动可选 Neo4j 服务并等待健康。
3. 通过 `docker compose exec neo4j cypher-shell -u neo4j -p '<口令>' -f /import/schema.cypher` 恢复 schema，再按项目导入清单执行数据脚本。
4. 若必须恢复管理员提供的 dump，在仓库外保存文件，停库后用 Neo4j Admin 在容器内恢复；不要把 dump 复制进受版本控制目录。

当前统一评审服务使用只读脱敏数据源。切换到生产 Neo4j 应通过部署环境注入连接信息，并复用现有 `trusted_graph_agent` / `new_role_discovery` 数据适配层；不得在镜像内写死地址或凭据。

## 测试与覆盖率

```bash
python -m pip install -r requirements-test.txt
python -m pytest
```

`pyproject.toml` 对统一部署入口启用分支覆盖率并设置 60% 最低门槛。容器同时包含镜像级和 Compose 级健康检查。

## 交付前检查

接收方只需要 Docker Desktop（Windows/macOS）或 Docker Engine + Compose 插件（Linux）。
代码包中应包含 `Dockerfile`、三个 Compose 文件、`.dockerignore`、`.env.example`、
`deployment_app.py`、两个前端静态目录和 `demo_data/`。不要包含本机 `.env`。

```powershell
docker compose config --quiet
docker compose up --build -d
docker compose ps
Invoke-RestMethod http://localhost:8080/healthz
docker compose down
```

健康状态应为 `healthy`，健康接口应返回 `status: ok`。默认模式下三个页面和相关 API
不依赖外部数据库、模型或第三方服务。
