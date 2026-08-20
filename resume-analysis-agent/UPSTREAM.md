# 上游来源

- Repository: `https://github.com/Kiana593/resume-analysis-agent`
- Branch: `main`
- Integrated commit: `883b79b38b1cb9716e41fb215e838a21b1834046`
- Retrieved: `2026-08-20`

当前环境直连 GitHub 的 `git clone` 超时，因此本目录通过 GitHub 仓库连接逐文件恢复了运行源码、测试、API 文档、岗位样例和核心数据文件。未恢复的内容仅包括 `archive/legacy/` 历史实现、拆分后的重复 FairCV 样例及 3 个二进制测试简历；这些内容不参与当前 API、MCP 或前端运行。

在可直连 GitHub 的环境中，可用上游仓库补齐这些非运行文件；整合改动集中在 `api_server.py`、`src/utils/llm.py`、`src/tools/visualize.py`、`pyproject.toml` 与 `tests/test_api_server.py`。
