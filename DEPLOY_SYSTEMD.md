# 生产部署（Linux + systemd + 现有 Nginx）

## 当前线上状态（2026-08-26）

- 统一地址：`https://talentgraphagent.site`
- 服务器：腾讯云轻量应用服务器 `49.233.65.202`（Ubuntu 22.04）
- HTTP 自动跳转 HTTPS；腾讯云防火墙仅为 Web 新增 TCP 443，应用内部端口只监听回环地址。
- Let's Encrypt 证书已启用，Certbot 定时续期及续期后 Nginx 重载已配置并通过 dry-run。
- `talentgraph-neo4j`、`talentgraph-graph`、`talentgraph-evolution`、`talentgraph-resume`、`talentgraph-frontend` 均已启用开机启动。
- 安装器会启用 `talentgraph-crawler.timer`：默认每 12 小时自动完成采集、清洗、增量归一化、图谱发布和岗位变化发现；单平台单轮最多选取 1500 条新增 JD。猎聘需要人工登录态，服务器无人值守任务会明确跳过该来源，不影响其他来源完成入图。
- 上线前回滚备份：`/var/backups/talentgraph-pre-20260826193416`。

本机 Windows 目录保留同一套安装器，后续版本可按下文流程无损更新服务器。

## 固定链路

```text
https://talentgraphagent.site
  ├─ /                 → 127.0.0.1:8090 统一前端
  ├─ /api/v1/evolution → 127.0.0.1:8070 岗位演化与人工审核
  ├─ /api/v1/resume    → 127.0.0.1:8000 简历 API（Nginx 去掉此前缀）
  └─ /api              → 127.0.0.1:8010 岗位图谱 API
                          └─ 127.0.0.1:7687 Neo4j Bolt
```

前端生产环境只使用同源路径；本地 `127.0.0.1` 页面固定使用 8000/8010/8070。不存在 URL 查询参数切换接口，也不使用 8072 或临时端口。

## 首次安装

在服务器准备：Ubuntu/Debian、root/sudo、已有 Nginx（安装器会复用并只写入自己的站点）、域名 `talentgraphagent.site` 的 DNS A/AAAA 记录，以及证书和私钥。证书路径可由 `TG_TLS_CERT`、`TG_TLS_KEY` 指定；默认读取 Certbot 路径。

```bash
cd /path/to/tiaozhan/trusted-job-graph-current
export TG_DOMAIN=talentgraphagent.site
export TG_TLS_CERT=/etc/letsencrypt/live/talentgraphagent.site/fullchain.pem
export TG_TLS_KEY=/etc/letsencrypt/live/talentgraphagent.site/privkey.pem
read -r -s NEO4J_PASSWORD
export NEO4J_PASSWORD
read -r -s IFLYTEK_SPARK_API_PASSWORD
export IFLYTEK_SPARK_API_PASSWORD
sudo --preserve-env=TG_DOMAIN,TG_TLS_CERT,TG_TLS_KEY,NEO4J_PASSWORD,NEO4J_USER,NEO4J_DATABASE,NEO4J_UNIT,IFLYTEK_SPARK_API_PASSWORD,IFLYTEK_SPARK_MODEL,IFLYTEK_SPARK_BASE_URL,TG_CRAWLER_INTERVAL,TG_CRAWLER_SCAN_MODE,TG_CRAWLER_PAGES,TG_CRAWLER_COLLECTION_LIMIT,TG_CRAWLER_PIPELINE_LIMIT,TG_CRAWLER_NEW_ROLE_LIMIT,TG_CRAWLER_ABILITY_CHANGE_LIMIT \
  bash deploy/install_systemd.sh
unset NEO4J_PASSWORD IFLYTEK_SPARK_API_PASSWORD
```

从当前 Windows 工作区制作无密钥、无原始数据的上线包：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_closed_loop_release.ps1
```

解压后应保留同级的 `trusted-job-graph-current/` 与 `爬虫代码/`，再从前者运行安装器。

已有线上环境升级时必须使用带自动回滚的入口，不直接运行安装器：

```bash
cd /path/to/unpacked-release/trusted-job-graph-current
sudo bash deploy/upgrade_systemd.sh
```

升级器会先将现网代码、systemd、Nginx 和非数据配置备份到
`/var/backups/talentgraph-pre-<时间>`。即时版本、服务或真实数据模式检查失败时会自动
恢复旧代码并重启服务；Neo4j 数据、审核记录、原始 JD 存档和演化状态始终不被覆盖。

安装器会创建 `/opt/talentgraph`、`/var/lib/talentgraph/evolution`、`/etc/talentgraph` 和 `/var/log/talentgraph`，同步代码时排除 `.env`、Neo4j 连接文件、原始 JD 与 `output/`，且不使用删除式同步。若 Neo4j 已在 `127.0.0.1:7474` 运行则不会替换它；否则使用持久化 Docker 容器 `talentgraph-neo4j`。

若现有 Neo4j 由原生 systemd 管理，请设置 `NEO4J_UNIT=实际的.service名称`（安装器也会尝试 `neo4j.service`），安装器会将该真实 unit 纳入 `talentgraph-neo4j.service` 的依赖和停止/启动链路；检测到 Neo4j 但找不到 unit 时会明确失败，不会用空操作服务伪装自动恢复。密码通过隐藏式 `read -s` 传入，安装器不会在输出中打印密码。

证书尚未就绪时，仅可显式用 `TG_HTTP_ONLY=1` 生成临时 HTTP 配置；它不满足上线验收，证书就绪后重新运行安装器即可启用 HTTP→HTTPS。

## 运维命令

```bash
sudo systemctl status talentgraph-neo4j talentgraph-graph talentgraph-evolution talentgraph-resume talentgraph-frontend
sudo systemctl status talentgraph-crawler.timer talentgraph-full-normalization.timer
sudo systemctl list-timers talentgraph-crawler.timer talentgraph-full-normalization.timer
sudo systemctl start talentgraph-crawler.service  # 立即执行一轮完整维护
sudo systemctl restart talentgraph-evolution       # 单服务重启
sudo systemctl restart talentgraph-graph talentgraph-resume talentgraph-frontend
sudo journalctl -u talentgraph-evolution -n 100 --no-pager
sudo tail -n 100 /var/log/talentgraph/evolution-error.log
sudo tail -n 100 /var/log/talentgraph/crawler-error.log
bash deploy/check_production.sh --base-url https://talentgraphagent.site
```

五个服务均配置 `Restart=always`（Neo4j 容器另有 `unless-stopped`），内部端口仅回环监听；安全组/防火墙只放行 22（按管理 IP 限制）、80、443。日志轮转由 `/etc/logrotate.d/talentgraph` 提供，API 访问日志关闭，应用日志不应包含密钥或用户原文。

自动维护使用独立 oneshot 服务，由 timer 持续触发；失败后 30 分钟重试，
最多连续重试两次。自动采集与每周日 03:30 的全量归一化共用
`/var/lib/talentgraph/maintenance.lock`，防止两个发布过程并发。默认采集参数可在
安装前通过 `TG_CRAWLER_INTERVAL`、`TG_CRAWLER_PAGES`、
`TG_CRAWLER_COLLECTION_LIMIT` 调整；讯飞密钥只写入权限为 0640 的环境文件。

## 回滚

上线前先备份 Nginx 站点、`/etc/talentgraph` 和演化状态目录：

```bash
sudo cp -a /etc/nginx/sites-available/talentgraph /etc/nginx/sites-available/talentgraph.$(date +%Y%m%d%H%M%S).bak
sudo cp -a /etc/talentgraph /var/backups/talentgraph-config-$(date +%Y%m%d%H%M%S)
sudo systemctl stop talentgraph-evolution talentgraph-graph talentgraph-resume talentgraph-frontend
```

回滚时将备份站点恢复、执行 `nginx -t && systemctl reload nginx`，再启动服务。Neo4j 数据、审核任务和演化结果位于独立目录，回滚代码不会删除它们；需要恢复数据库时只使用管理员确认过的备份，禁止覆盖式加载未经核对的 dump。

## 验收范围

`check_production.sh` 检查 HTTPS、HTTP 跳转、统一前端、三类 API、Neo4j 可用性、审核存储、内部端口和 systemd 状态，并以无效候选审核 POST 验证审核写入路由可达（不创建任务、不发送用户数据）。浏览器验收需在服务器域名下打开 `emerging-roles.html`，从其他页面切回后确认真实候选和人工审核按钮仍可用。

验收还会同时核对前端、图谱、简历和岗位演化服务的发布指纹
`closed-loop-2026.08`。任一服务仍是旧版或演示数据模式都会失败；维护状态必须存在
最近一次成功闭环，且未超过计划周期的两倍（最低容忍 24 小时）。因此“页面能打开”
不再等同于上线成功。
