# html-main2 统一前端

这个目录保留原有页面功能，并生成了一套统一风格的前端页面。原仓库文件没有被修改。

## 文件说明

| 文件 | 说明 |
| --- | --- |
| `index.html` | 系统总览入口 |
| `panorama.html` | 统一版岗位能力图谱，继承图谱 API、筛选、缩放、证据追溯 |
| `emerging-roles.html` | 统一版新岗位发现工作台，保留候选岗位审核主流程，并呈现新增与能力更新信号 |
| `resume-match.html` | 人岗差距分析工作台，对接 `resume-analysis-agent` 的 FastAPI |
| `assets/ui.css` | 统一样式规范 |
| `assets/ui.js` | 通用请求、格式化、转义和导航工具 |
| `source/` | 原始 HTML 备份，不要直接改这里 |

## 打开方式

静态页面可直接双击 `index.html`、`emerging-roles.html` 查看。

队长新增的岗位变化信息已经进入 `emerging-roles.html` 的新岗位发现主流程，并统一标注：

- `新增岗位`
- `新兴细分`
- `新增能力`
- `能力增强`
- `能力减少`

数据库明细、原始 JD 和回标证据暂时只保留接入位，后续拿到完整图谱服务或演化接口后再替换数据源。

原始独立页面保留在 `source/role-updates.original.html`，只作追溯，不作为主入口。

`panorama.html` 需要图谱服务提供 API。先在 `trusted-job-graph-current` 启动：

```powershell
python display_graph_handoff.py serve --neo4j-config config\neo4j_connection.json
```

然后打开 `http://127.0.0.1:8090/panorama.html`。页面默认调用 `http://127.0.0.1:8010/api/...`；也可用 `?graphApi=http://server:8010` 覆盖地址。

`resume-match.html` 需要简历分析服务：

```powershell
cd resume-analysis-agent
.\.venv\Scripts\python.exe api_server.py
```

页面默认调用 `http://127.0.0.1:8000`，也可用 `?resumeApi=http://server:8000` 覆盖地址。项目根目录的 `start_demo.py` 可以统一启动前端与后端。

## 后续修改建议

- 新增页面时先复用 `assets/ui.css` 和 `assets/ui.js`。
- 表格中的数字列使用 `class="num"`。
- 主操作放在右侧 `.actions.right`。
- 长文本用 `.truncate` 或 `.clamp-2`，详情放到侧栏或详情区域。
- 不要在新页面里重新定义一套颜色、按钮和卡片样式。
- 新增与能力更新标签使用固定语义：绿色表示新增，蓝色表示增强或新兴细分，橙色表示减少。
