# 项目整合说明

当前集成由三部分组成：

- 仓库根目录：岗位图谱与 Neo4j API，端口 `8010`
- `resume-analysis-agent/`：简历解析、七维人岗匹配与差距分析 API，端口 `8000`
- `qianduan/html-main2/`：统一前端，端口 `8090`

版本边界：本目录是当前主版本；本地完整 Neo4j 由 `config/neo4j_connection.json` 指向，展示部署版由 `display_graph_handoff.py export` 单独生成。详细目录约定见 [LOCAL_VERSION_MAP.md](LOCAL_VERSION_MAP.md)。

前端的 `panorama.html` 调用岗位图谱 API，`resume-match.html` 调用简历分析 API；首页同时检查两项服务。首页“新岗位动态雷达”会调用图谱 API，在后台依次执行限量采集、增量入图、活动图谱发布和新岗位发现。简历页传入的模型密钥只用于当前 HTTP 请求，不会写入 `.env` 或磁盘。

## 首次配置

简历服务的 Python 3.12 虚拟环境已经位于 `resume-analysis-agent/.venv`。重新安装时执行：

```powershell
cd resume-analysis-agent
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
cd ..
.\resume-analysis-agent\.venv\Scripts\python.exe -m pip install -r requirements-crawler.txt
```

岗位图谱页需要 Neo4j。新环境可复制示例并填写本地连接配置：

```powershell
Copy-Item config\neo4j_connection.example.json config\neo4j_connection.json
```

如果数据库尚无展示数据，请先按 `qianduan/html-main2/REAL_SETUP.md` 导入 `display_graph.json`。

## 启动

在项目根目录执行：

```powershell
.\resume-analysis-agent\.venv\Scripts\python.exe start_demo.py
```

也可以使用明确的本地完整库入口：

```powershell
.\scripts\check_local_full.ps1
.\scripts\start_local_full.ps1
```

打开 `http://127.0.0.1:8090/index.html`。只检查目录和服务状态：

```powershell
.\resume-analysis-agent\.venv\Scripts\python.exe start_demo.py --check
```

当 Neo4j 配置可用时，启动器还会自动为简历 API 设置 `STORE_BACKEND=neo4j`，因此岗位粗排直接使用图谱中的真实标准岗位，而不是 6 个内存示例。
如果本机 `7687` 端口尚未监听，启动器会使用配置中的 `instance_dir` 和 `java_home` 自动启动对应的 Neo4j Desktop 实例，并等待数据库就绪后再启动两个 API。

### 首页动态雷达

首页监测入口只有两种方式：

- **查看已有图谱**：直接读取本地 Neo4j 中已经处理好的岗位、技能和趋势数据，不重新采集。
- **在线测试（200 条 JD）**：使用智联招聘单一岗位方向，最多采集 200 条 IT JD，调用讯飞 Spark Lite 生成一轮测试结果；测试不会切换正式归一化图谱。

每次只允许一个任务运行。在线测试会写入本地审计/运行产物，正式展示图谱仍以已有活动版本为准。

前端会轮询以下接口并显示真实阶段进度：

- `POST http://127.0.0.1:8010/api/v1/radar/runs`
- `GET http://127.0.0.1:8010/api/v1/radar/status`
- `GET http://127.0.0.1:8010/api/v1/radar/config`
- `GET http://127.0.0.1:8010/api/v1/radar/results/latest`

雷达接入沿用保护式流水线，只有采集、处理和校验全部成功才发布活动图谱；发布成功后自动运行新岗位与旧岗位能力变化发现，“新岗位发现”页会优先读取这份最新结果，没有完成过任务时才保留前端预览候选。能力抽取需要在启动统一服务的同一终端中配置讯飞环境变量：

新岗位发现沿用既有的全量时间窗口流程：默认至少 3 条 JD、3 家企业、3 个模板、3 项能力和2项共享能力，并要求连续月份或独立来源证据达到原机械门槛。职责原文继续作为 AI 与人工审核材料，但不再作为机械候选准入条件。`REVIEW` 与 `WATCH` 都可进入人工查看，区别用于提示证据强弱。来源分布漂移仍作为质量警告，不会把平台差异直接当成岗位涌现。

新岗位页面只使用 `8090/emerging-roles.html`；访问岗位演化 API 的 `/new-roles` 会跳转到该融合页面。Spark Lite 只是前 K 个候选的分析参与者，提供分类、标准名和岗位边界草稿；AI 缺失、部分完成、认为是别名或摘要措辞不理想，都不会隐藏或淘汰规则候选。人工审核通过 `8070/api/v1/evolution/.../review` 写入 Neo4j 独立版本子图，只有人工决定才改变候选状态；存储不可用时前端会停用按钮。

定时任务与岗位演化服务统一读取 `EVOLUTION_DATA_ROOT`。生产环境均指向 `/var/lib/talentgraph/evolution`，因此定时任务完成后，新岗位页面和演化 API 会严格读取同一份最新全量任务；最新结果为 0 时直接展示 0，不回退到旧任务。

```powershell
$env:IFLYTEK_SPARK_API_PASSWORD = "控制台中的 APIPassword"
$env:IFLYTEK_SPARK_MODEL = "对应的模型 ID"
.\resume-analysis-agent\.venv\Scripts\python.exe start_demo.py
```

前程无忧与猎聘可能要求有效登录态；若平台拦截或密钥缺失，首页会停止在失败状态并展示日志末行摘要，不会切换活动图谱。

如 Neo4j 配置在其他位置：

```powershell
.\resume-analysis-agent\.venv\Scripts\python.exe start_demo.py `
  --neo4j-config "D:\path\to\neo4j_connection.json"
```

## API 地址策略

当前版本固定为单一链路，不支持通过 URL 查询参数覆盖 API：本地页面固定访问
`127.0.0.1:8000`、`127.0.0.1:8010`、`127.0.0.1:8070`；生产页面使用同源
`/api/v1/resume`、`/api`、`/api/v1/evolution`，由现有反向代理转发。
