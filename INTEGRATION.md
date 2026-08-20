# 项目整合说明

当前集成由三部分组成：

- 仓库根目录：岗位图谱与 Neo4j API，端口 `8010`
- `resume-analysis-agent/`：简历解析、七维人岗匹配与差距分析 API，端口 `8000`
- `qianduan/html-main2/`：统一前端，端口 `8090`

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

打开 `http://127.0.0.1:8090/index.html`。只检查目录和服务状态：

```powershell
.\resume-analysis-agent\.venv\Scripts\python.exe start_demo.py --check
```

当 Neo4j 配置可用时，启动器还会自动为简历 API 设置 `STORE_BACKEND=neo4j`，因此岗位粗排直接使用图谱中的真实标准岗位，而不是 6 个内存示例。
如果本机 `7687` 端口尚未监听，启动器会使用配置中的 `instance_dir` 和 `java_home` 自动启动对应的 Neo4j Desktop 实例，并等待数据库就绪后再启动两个 API。

### 首页动态雷达

首页默认采用“全量关键词池”：统一读取 `config/job_radar_keywords.json` 中维护的 73 个岗位关键词，覆盖北上广深，并支持三平台联合巡检。也可以选择 12 个代表岗位的快速抽样，或从关键词池选择一个岗位定向检查。每次只允许一个任务运行，本轮总 JD 上限为 `20–2000` 条；三平台联合时会把上限均分到各平台，达到目标后停止继续遍历关键词，并只把本轮新增数据的受限 CSV 快照送进入图流程。

前端会轮询以下接口并显示真实阶段进度：

- `POST http://127.0.0.1:8010/api/v1/radar/runs`
- `GET http://127.0.0.1:8010/api/v1/radar/status`
- `GET http://127.0.0.1:8010/api/v1/radar/config`
- `GET http://127.0.0.1:8010/api/v1/radar/results/latest`

雷达接入沿用保护式流水线，只有采集、处理和校验全部成功才发布活动图谱；发布成功后自动运行新岗位与旧岗位能力变化发现，“新岗位发现”页会优先读取这份最新结果，没有完成过任务时才保留前端预览候选。能力抽取需要在启动统一服务的同一终端中配置讯飞环境变量：

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

## 可配置 API 地址

默认使用 `8000` 与 `8010`。部署或联调其他地址时可通过查询参数覆盖：

```text
http://127.0.0.1:8090/resume-match.html?resumeApi=http://server:8000
http://127.0.0.1:8090/panorama.html?graphApi=http://server:8010
```
