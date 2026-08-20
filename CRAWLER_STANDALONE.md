# 三平台独立定时采集

`job_crawler_runner.py` 将前程无忧、智联招聘和猎聘三个现有爬虫统一为一个运行入口。默认只写入项目同级的 `crawler_standalone_output/`；加 `--system-import` 后才接入原始审计层和图谱处理，加 `--system-publish` 后才切换活动图谱版本。

先安装爬虫运行依赖：

```powershell
pip install -r requirements-crawler.txt
```

小范围查看执行计划（单岗位、单城市、单页，不发请求）：

```powershell
python job_crawler_runner.py run --keyword 产品经理 --city 北京 --pages 1 --dry-run
```

小范围实际试跑：

```powershell
python job_crawler_runner.py run --keyword 产品经理 --city 北京 --pages 1
```

统一关键词池巡检（73 个岗位关键词、北上广深，每个平台新增 JD 达到 300 条后停止继续遍历）：

```powershell
python job_crawler_runner.py run --scan-mode full --pages 1 --collection-limit 300
```

快速抽样巡检使用关键词池中的 12 个代表岗位：

```powershell
python job_crawler_runner.py run --scan-mode quick --pages 1 --collection-limit 100
```

`--scan-mode target` 必须与 `--keyword` 一起使用，且关键词必须来自 `config/job_radar_keywords.json`。旧命令不传 `--scan-mode` 时保持原有行为。

全量每 12 小时重扫一次：

```powershell
python job_crawler_runner.py schedule --interval-minutes 720
```

全量采集成功后接入系统并统一发布一次：

```powershell
python job_crawler_runner.py schedule --interval-minutes 720 --system-import --system-publish
```

将已有三个 CSV 直接接入系统，不重新爬取：

```powershell
python job_crawler_runner.py run --reuse-output --system-import --system-publish
```

每个平台使用固定状态目录，CSV 会按职位键去重；每轮生成单独的日志和 `manifest.json`。接入时先导入原始数据并完成 IT 岗位准入，只对通过准入的岗位调用讯飞星火 HTTP 大模型提取五维能力，再由本地程序执行 JSON 结构校验、噪声过滤和 JD 原文证据回查；没有原文证据的模型输出会被丢弃。成功结果写入断点缓存，重复 JD 不会重复请求。系统接入采用保护式两段流程：各平台分别完成原始导入、IT 准入和能力处理，全部成功后统一归一化并最多发布一次。任一阶段失败都会阻止最终发布。

运行前只需把密钥放入当前终端环境；模型和地址可按讯飞控制台实际值覆盖：

```powershell
$env:IFLYTEK_SPARK_API_PASSWORD = "控制台中的 APIPassword"
$env:IFLYTEK_SPARK_MODEL = "该 APIPassword 对应的模型 ID"
$env:IFLYTEK_SPARK_BASE_URL = "https://spark-api-open.xf-yun.com/v1"
```

可用 `--platform 51job`、`--platform zhilian`、`--platform liepin` 单独运行，也可重复提供该参数。使用 `--help` 查看其余选项。
