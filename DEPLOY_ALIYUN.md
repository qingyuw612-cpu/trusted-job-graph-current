# 阿里云 ECS 生产上线指南

把「可信岗位图谱」部署到阿里云 ECS，并用真实的 Neo4j dump 数据对外提供服务。

## 前置条件

| 事项 | 说明 |
|---|---|
| ECS | 大陆地域、**包年包月**（按量付费拿不到备案服务号）、≥ 2 vCPU / 4 GB 内存（推荐 8 GB） |
| 数据盘 | 一块独立云盘（≥ 50 GB），挂载到 `/data`，专门放 Neo4j 数据和 dump |
| 域名（可选） | 大陆地域用域名访问必须完成 ICP 备案（约 1～3 周，可与部署并行） |
| dump | 你的 `.dump` 文件（Neo4j 5.x 的 `neo4j-admin database dump` 产物） |

## 目录约定

- `/data/neo4j` —— Neo4j 数据目录（图数据库本体，落在数据盘）
- `/data/backups` —— dump 原件和备份（`NEO4J_IMPORT_DIR`）
- `./output/role_evolution_workbench_v2` —— 新岗位发现/能力变化的演化产物（可选，不在 dump 里）

## 快速开始（三步）

```bash
# 0) 首次：装 Docker、挂载数据盘（见下方「环境准备」）

# 1) 初始化：建目录、生成 .env 和连接配置
./deploy.sh init

# 2) 恢复数据：把 dump 放进 /data/backups 后
./deploy.sh restore /data/backups/your.dump

# 3) 启动应用
./deploy.sh up
```

验证：

```bash
curl http://127.0.0.1:8080/healthz
# 期望 backend 为 "neo4j"（而非 "sanitized_demo"），说明连上了真实数据
```

## 环境准备（首次）

```bash
# 装 Docker
curl -fsSL https://get.docker.com | sh
sudo systemctl enable --now docker

# 挂载数据盘（假设设备是 /dev/vdb，用 lsblk 确认）
sudo mkfs.ext4 /dev/vdb
sudo mkdir -p /data
sudo mount /dev/vdb /data
echo '/dev/vdb  /data  ext4  defaults  0 0' | sudo tee -a /etc/fstab

# 把项目代码放到服务器上（克隆或 scp）
# 进入项目目录后：
chmod +x deploy.sh
```

## 环境变量（`.env`）

`./deploy.sh init` 会自动生成 `.env` 并附带一个随机 Neo4j 密码。可手动改：

| 变量 | 默认 | 说明 |
|---|---|---|
| `NEO4J_AUTH` | `neo4j/<随机密码>` | Neo4j 账号密码 |
| `APP_PORT` | `8080` | 应用对外端口 |
| `NEO4J_DATA_DIR` | `/data/neo4j` | Neo4j 数据目录 |
| `NEO4J_IMPORT_DIR` | `/data/backups` | dump/导入目录 |
| `EVOLUTION_DATA_PATH` | `./output/role_evolution_workbench_v2` | 演化产物目录 |
| `BASE_IMAGE` | `python:3.12-slim` | Docker Hub 不可用时改成微软镜像 |

改完 `.env` 后，若涉及密码，需重跑 `./deploy.sh init` 同步 `config/neo4j_connection.json`。

## 安全组（阿里云控制台）

只放行：`22`（SSH，建议限定自己 IP）、`80`/`443`（HTTP/HTTPS）。
**不要**对外开放 `7474`/`7687`——compose 已把它们绑定到 `127.0.0.1`，仅服务器内部可访问。

## 对外访问（80/443 与 HTTPS）

应用跑在 `8080`。两种方式：

1. **快速（IP 访问，无 HTTPS）**：`.env` 里设 `APP_PORT=80`，安全组放行 80，用 `http://公网IP` 访问。
2. **正式（域名 + HTTPS）**：Nginx 监听 80/443，反代到 `127.0.0.1:8080`；证书用阿里云 SSL 证书（免费 DV）或 Let's Encrypt。

## 常用命令

```bash
./deploy.sh status   # 服务状态 + 健康接口
./deploy.sh logs app  # 应用日志
./deploy.sh logs neo4j
./deploy.sh down     # 停止（数据保留在 /data/neo4j）
```

## 注意事项

- **密码同步**：`.env` 的 `NEO4J_AUTH` 与 `config/neo4j_connection.json` 必须一致；`./deploy.sh init` 会统一生成。
- **dump 恢复会覆盖**：`neo4j-admin database load --overwrite-destination=true` 会覆盖目标库，重复执行前先确认。
- **演化产物不在 dump 里**：`new-roles` / `ability-changes` 两个页面依赖 `output/role_evolution_workbench_v2/` 里的 JSON 产物；如果只有 Neo4j dump，这两个页面会是空的，全景图（`/`）不受影响。
- **备份**：dump 原件保留在 `/data/backups`；升级前建议再 `docker compose -f compose.prod.yaml exec neo4j neo4j-admin database dump neo4j --to-path=/data/backups` 做一次新备份。
- **敏感文件不入库**：`.env`、`config/neo4j_connection.json`、`.dump`、`/output/` 均已被 `.gitignore` 排除。
