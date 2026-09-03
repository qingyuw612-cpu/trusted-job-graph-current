# TalentGraph Agent 评审交付说明

## 版本对应关系

- 线上地址：<https://talentgraphagent.site>
- 主代码仓库：<https://github.com/qingyuw612-cpu/trusted-job-graph-current>
- 评审版本标签：`production-2026-09-03`
- 当前前端锁：`closed-loop-2026.09`
- 前端唯一源目录：`qianduan/html-main2/`

本交付版本以线上展示版本为准，仓库中的 Docker 评审模式使用脱敏演示数据，
不包含真实 Neo4j 数据库、原始 JD、模型权重、API Key 或服务器密码。

## 运行方式

```bash
docker compose up --build -d
```

启动后访问 <http://localhost:8080/>，健康检查地址为
<http://localhost:8080/healthz>。

## 测试与覆盖率

```bash
python -m pytest
```

本版本已验证：182 项测试通过，统一部署入口覆盖率为 65.70%，满足不低于 60% 的要求。

## 交付边界

- 源代码：本仓库中受 `.gitignore` allowlist 管理的源码、配置示例、部署文件和测试用例。
- 可执行程序：由 `Dockerfile` 构建的容器镜像；项目不提供独立桌面 `.exe`。
- 部署说明：见 `DEPLOYMENT.md`、`DEPLOY_ALIYUN.md` 和 `deploy/`。
- 生产 Neo4j 连接、密钥、原始数据和数据库备份不进入 Git 仓库。
