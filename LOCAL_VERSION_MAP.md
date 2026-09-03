# 本地版本与数据边界

## 最终前端锁（2026-09-03）

当前线上与本地唯一最终前端版本为 `closed-loop-2026.09`，唯一源目录是
`trusted-job-graph-current/qianduan/html-main2`。发布前必须运行
`scripts/check_final_frontend.ps1`；文件与 `deploy/FINAL_FRONTEND_RELEASE.json`
不一致时禁止构建和上线。不得再从同级 `qianduan`、`hackathon-final-demo`、
历史压缩包或服务器备份中覆盖当前前端。

从现在开始，`trusted-job-graph-current` 是唯一主代码目录。不要再从同级的旧快照目录启动服务。

## 版本划分

| 名称 | 目录/入口 | 数据范围 | 用途 |
| --- | --- | --- | --- |
| 本地完整分析版 | 本目录 + `config/neo4j_connection.json` | 完整 Neo4j：原始 JD、处理结果、能力关系、证据链、归一化版本 | 本地分析、论文评估、全量新岗位发现 |
| 展示部署版 | `display_graph_handoff.py export` 生成的 `deploy/display-package/` | 仅岗位、岗位族、技能、画像、时间快照和聚合关系 | 对外演示、部署上线 |
| 旧快照 | `D:\qing\tiaozhan\可信岗位图谱Agent` | 旧代码和旧配置，不与当前运行环境同步 | 仅作历史参考，不作为启动入口 |
| 运行产物 | `output/`、`crawler_standalone_output/` | 分析结果、采集快照、日志、任务状态 | 可清理或归档，不是代码版本 |
| 历史交付物 | `deploy/`、根目录 `*.tar.gz` / `*.zip` | 某次导出的部署包 | 不作为当前源代码或数据库 |

## 本地完整数据库

完整数据不放进 Git，也不复制进部署包。当前完整库是 Neo4j Desktop 中由
`config/neo4j_connection.json` 指向的 DBMS，启动器会按该配置连接或启动它。

当前最近一次核验结果（2026-08-22）：

- 新岗位发现数据源：108,251 条可用 IT JD；
- 岗位图谱 API：108,281 条 JD、84 个岗位、368 个归一化技能；
- 活动归一化版本：`normalization:547e6aa57fd59bdca01e`；
- 正在导入/处理任务：0。

数据量以后变化时，以 `scripts/check_local_full.ps1` 的实时结果为准，不手工修改本文档中的数字。

## 正确操作

在本目录执行：

```powershell
.\scripts\check_local_full.ps1
.\scripts\start_local_full.ps1
```

从完整本地库生成部署展示包：

```powershell
.\scripts\export_display_package.ps1
```

展示包明确不包含原始 JD、真实公司、处理中间节点和证据原文。需要完整原文回标时，必须回到本地完整分析版，不能使用展示库反推。

## 禁止混用

- 不要从 `可信岗位图谱Agent` 启动当前服务。
- 不要把 `display_graph.json` 导入本地完整库；展示包应导入单独的空 Neo4j 数据库。
- 不要把 `output/` 或 `crawler_standalone_output/` 当成 Neo4j 数据库备份。
- 不要把 `config/neo4j_connection.json`、Neo4j dump、原始 JD 或 API 密钥提交到 Git。
