# 队员前端联调说明

这个仓库中的岗位能力全景页需要后端数据，不能直接双击 HTML 完整运行。仓库提供 `display_graph_handoff.py`，用于在不分享原始招聘数据的前提下导入并启动精简展示图谱。

## 需要从项目负责人处取得

私下取得 `trusted-job-graph-display.zip`。它不上传到公开 GitHub，也不包含原始 JD、真实公司、处理复核记录、能力候选或证据原文。

## 本地运行

1. 克隆仓库并进入目录。
2. 解压 `trusted-job-graph-display.zip`。
3. 在 Neo4j 中新建一个**空数据库**，建议命名为 `trusted-job-graph-demo`。
4. 复制 `config/neo4j_connection.example.json` 为 `config/neo4j_connection.json`，填写这个空数据库的连接信息。
5. 导入数据：

```powershell
python display_graph_handoff.py import `
  --package "C:\path\to\display_graph.json" `
  --neo4j-config config\neo4j_connection.json
```

6. 启动前端：

```powershell
python display_graph_handoff.py serve `
  --neo4j-config config\neo4j_connection.json
```

7. 浏览器访问 <http://127.0.0.1:8010/>。

如需确认数据库没有混入敏感或中间数据：

```powershell
python display_graph_handoff.py verify `
  --neo4j-config config\neo4j_connection.json
```

## 展示包包含什么

- 岗位、岗位族和岗位别名
- 归一化技能
- 岗位画像、行业、职级和时间窗口
- 正式活动版本的岗位核心技能
- 岗位技能时间快照

## 展示包不包含什么

- 原始 JD、职位描述、薪资和招聘链接
- 公司名称和公司节点
- `RawJDVersion`、`ProcessedJD`、`AbilityCandidate`、`ProcessingReview`
- 技能证据原文
- 新岗位发现所需的全量原始和处理中间数据

因此，这个包适合岗位能力全景页面的视觉统一和交互联调，不用于重新运行完整数据处理或新岗位发现算法。技能证据详情为空属于预期行为。
