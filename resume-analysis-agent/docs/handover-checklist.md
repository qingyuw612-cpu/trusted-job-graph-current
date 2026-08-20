# 交接清单 — 简历匹配 agent → 前端联调

> 口径：核心 schema 为 **7 维**（knowledge/skill/qualifications/preference/motivation/trait/self_concept），严格对齐 Neo4j 图谱 `NormalizedSkill.category`；大纲"五分类"仅为原始数据/汇报口径，提供 7→5 投影用于对外材料。
>
> 已具备（不在本清单内）：PDF/DOCX 解析、7 维 LLM 提取 + 防幻觉校验、批量多线程、粗排 + 语义复核、差距分析 + 学习路径、雷达图、MCP/CLI 双模式、86 项测试。

## A. 口径与契约（先定，所有交付物的前提）

- [x] **A1 维度口径定案（7 维对齐图谱）**
  - 与二作确认 Neo4j `category` 集合就是 7 个（知识/技术/任职条件/招聘偏好/动机/特质/自我概念）
  - 补 `DIM_TO_CATEGORY` 反查表；写 7→5 投影函数（仅对外材料/大纲口径用）
  - 删除五维死代码（`src/prompts/__init__.py`、`src/prompts/gap_analysis.py` 里的 `GAP_ANALYSIS_PROMPT`，保留 7 维 `ROLE_GAP_PROMPT`）
  - README/AGENTS 写明"核心口径 7 维，五分类为原始数据/大纲口径"
  - 验收：代码无五维残留、投影函数有单测、文档有口径说明
  - 完成记录：DIM_TO_CATEGORY + project_to_five_dim（任职条件→知识、招聘偏好→动机）已实现并有单测；删除双份 GAP_ANALYSIS_PROMPT 及 legacy LEARNING_PATH_PROMPT；ROLE_GAP_PROMPT 补全 7 维（原缺 preference）；README/AGENTS 增口径章节；mcp 资源 dimensions://seven 修正 category 显示并注明五分类仅汇报口径。94 测试通过（含新增 9 项）。

- [x] **A2 图谱 API 契约对齐**
  - 与二作定 role 画像 schema：字段集合、必备/加分技能区分、领域/岗位族、JD 数量
  - 确认 45/134 覆盖问题（哪些岗位能匹配、前端空匹配提示方案）
  - 验收：契约文档 + 一份样例岗位画像 JSON
  - 完成记录：docs/graph-contract.md（schema + 45/134 岗位清单 + 空匹配提示方案）+ docs/sample-role-profile.json（Java开发工程师，真实图谱数据）；待二作确认项集中在契约 §7（role_id/evidence_count/domain_name 损坏/taxonomy 版本/必备加分阈值）。

## B. 交付物（给五作前端联调）

- [x] **B1 HTTP API 封装（FastAPI）**
  - 路由：`/health`、`/upload`、`/extract`、`/rank`、`/enhance`、`/gap`、`/modify`、`/radar`
  - 复用 `src/tools/` 逻辑，不复制业务代码；swagger 自动文档
  - 验收：每条路由有示例请求/响应，前端本地能起服务
  - 完成记录：api_server.py 8 路由已端到端验证（neo4j 后端 75 岗位；upload/rank/radar 纯逻辑全通；extract/enhance/gap/modify 用真实 DeepSeek deepseek-v4-flash 验证通过）；docs/api.md 逐路由示例；无凭证时可用 tests/mock_llm_server.py 离线联调。
  - 追加（2026-08-11）：LLM 配置重构为并列 Switch 模式（每供应商独立 {PROVIDER}_* 块 + LLM_PROVIDER 切换，废弃共享 LLM_API_KEY/MODEL/BASE_URL），见 src/utils/llm.py、.env.example、README/AGENTS/deploy.md。

- [x] **B2 结构化 gap 响应 schema**
  - 含：匹配结论、7 维得分结构（0-1 或命中/缺失）、缺失技能清单（按重要性排序）、学习路径、整体建议
  - 验收：JSON schema 文档 + 一份真实简历的完整样例输出
  - 完成记录：analyze_gap 新增结构化 report（build_gap_report 纯逻辑 + 4 项单测）；docs/gap-schema.md（含 JSON Schema）+ docs/sample-gap-response.json（真实简历+图谱 rank 数据）。

- [x] **B3 学习路径 schema（gap 的子契约，单列）**
  - 字段固定：`step / skill / importance / prerequisite / resources / estimated_effort / why`
  - 验收：前端学习路径卡片能按此渲染，无需解析 Markdown
  - 完成记录：ROLE_GAP_PROMPT 学习路径 schema 升级为 7 字段；build_gap_report 规范化缺失字段；docs/learning-path-schema.md + docs/sample-learning-path.json。

- [x] **B4 部署说明 + 端到端 demo**
  - 启动文档（依赖、`.env`、Neo4j 配置、命令）、示例 curl 序列
  - 用一份真实简历跑通 `upload → extract → rank → gap → radar` 全链路
  - 验收：前端照文档操作即可拿到完整 JSON 并出图
  - 完成记录：docs/deploy.md（依赖/.env/Neo4j/启动/curl 序列/配置坑）；真实简历 faircv_000 全链路产物 results/_demo_{upload,extract,rank,gap}.json + _demo_radar.png（LLM 步骤用真实 DeepSeek 跑通，mock 可离线复现）。

## C. 移交后可并行（不阻塞联调）

- [x] C1 加权评分：`score = (Σ命中技能 final_score / Σ全部技能 final_score) × min(1, 技能数/10)`
  - final_score 取图谱 HAS_CORE_SKILL 边权重（JD 支持度），零权重回退纯命中率；
  - IDF 默认关闭（`use_idf` 开关消融）；维度权重不叠加；rank 响应 schema 不变；
  - 测试 108 通过；真实简历（后端 Java 应届生）A/B：新分 Java 0.497 > Go 0.336 >
    Android 0.258 > 嵌入式 0.169，排序合理且分差拉开；旧公式下 Android(0.25) 反超
    Go(0.23)，判别力弱。
  - 复核口径统一：apply_enhance_review 按同一加权公式重算（rank 结果携带 skill_weights），
    粗排/复核分数一致；LLM 复核 top10 实测 top3 稳定（Java/Go/Android）。
  - 后续增强（不阻塞交接）：rank 未使用简历"求职意向/目标岗位"锚点，可加 position
    锚定或由前端按目标岗位筛选。
- [ ] C2 准确率评测 ≥90%：20 份真实简历标注 + 提取准确率脚本；配合四作 100 条 JD 测试集算匹配准确率
- [ ] C3 反馈循环：匹配结果满意度/纠错反馈接口
- [ ] C4 真实简历数据收集（脱敏）
- [ ] C5 PPT/演示素材输出给五作

## 执行顺序

A1 → A2 → B1 → B2/B3 → B4。A1 最快（半天内），做完维度口径不再摇摆；A2 依赖二作，可并行确认；B 组做完即可交接。

