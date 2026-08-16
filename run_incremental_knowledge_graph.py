"""手动运行新增数据的增量知识图谱流水线。

该入口只编排已有模块，不复制清洗、能力分析、关键词归一化或 Neo4j
写入逻辑。默认完成原始数据导入、能力处理和归一化检查；加 --publish
才会把归一化结果发布为 Neo4j 当前活动版本。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable


def run_step(label: str, script: Path, *args: str) -> None:
    print(f"\n===== {label} =====", flush=True)
    command = [PYTHON, "-B", str(script), *args]
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)


def run_module_step(label: str, module: str, *args: str) -> None:
    print(f"\n===== {label} =====", flush=True)
    command = [PYTHON, "-B", "-m", module, *args]
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)


def latest_discovery_task(data_root: Path) -> dict:
    jobs_path = data_root / "role_evolution_jobs" / "jobs.json"
    if not jobs_path.exists():
        return {}
    tasks = json.loads(jobs_path.read_text(encoding="utf-8"))
    if not isinstance(tasks, list) or not tasks:
        return {}
    task = max(
        tasks,
        key=lambda row: str(
            row.get("completed_at") or row.get("started_at") or row.get("created_at") or ""
        ),
    )
    return {
        "task_id": task.get("task_id"),
        "run_id": task.get("run_id"),
        "task_status": task.get("status"),
        "parameters": task.get("parameters") or {},
        "summary": task.get("summary") or {},
        "warnings": task.get("warnings") or [],
        "error": task.get("error") or "",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="增量运行：原始数据导入 → 能力分析回标 → 关键词归一化 → Neo4j发布 → 岗位与能力变化发现"
    )
    parser.add_argument("--source", type=Path, help="新增 CSV/JSON/JSONL 文件或所在目录；默认使用项目配置目录")
    parser.add_argument(
        "--platform",
        default="",
        help="来源平台名称；新平台或跨平台同名文件必须填写，例如：猎聘",
    )
    parser.add_argument("--neo4j-config", type=Path, help="Neo4j 连接配置文件")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条当前版本；0 表示全部")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--processing-batch-size",
        type=int,
        default=200,
        help="能力处理单事务记录数；独立于原始导入批次，默认 200",
    )
    parser.add_argument("--llm-endpoint", default="", help="可选：已有能力分析缺失时使用的兼容 Webhook")
    parser.add_argument(
        "--iflytek-spark",
        action="store_true",
        help="领域准入后仅对 IT 岗位调用已配置的讯飞星火模型",
    )
    parser.add_argument("--skip-import", action="store_true", help="数据已经在原始层时跳过导入")
    parser.add_argument("--force-import", action="store_true", help="同一文件曾按旧字段规则导入时强制重新适配")
    parser.add_argument("--skip-normalization", action="store_true", help="只导入并处理能力，不做向量归一化")
    parser.add_argument("--publish", action="store_true", help="发布归一化结果并切换 Neo4j 活动版本")
    parser.add_argument(
        "--skip-new-role-discovery",
        action="store_true",
        help="发布活动图谱后跳过岗位与能力变化发现阶段",
    )
    parser.add_argument(
        "--new-role-llm-mode",
        choices=("off", "auto", "required"),
        default="auto",
        help="新岗位候选语义复核模式；默认 auto，无密钥时保留规则候选",
    )
    parser.add_argument(
        "--new-role-limit",
        type=int,
        default=50,
        help="每次最多送入语义复核的新岗位候选数",
    )
    parser.add_argument(
        "--ability-change-limit",
        type=int,
        default=5,
        help="每次最多送入语义复核的旧岗位能力变化候选数；默认 5，设为 0 可关闭",
    )
    parser.add_argument(
        "--new-role-timeout-seconds",
        type=int,
        default=1800,
        help="自动发现阶段最长等待秒数",
    )
    parser.add_argument(
        "--new-role-data-root",
        type=Path,
        default=PROJECT_ROOT / "output" / "role_evolution_workbench_v2",
        help="新岗位候选、报告和审核队列的保存目录",
    )
    parser.add_argument(
        "--keep-work-db",
        action="store_true",
        help="发布成功后仍保留临时 SQLite；默认自动删除以避免重复占用空间",
    )
    parser.add_argument("--work-dir", type=Path, default=PROJECT_ROOT / "output" / "processed_normalization_incremental")
    args = parser.parse_args()

    neo4j_config = args.neo4j_config.resolve() if args.neo4j_config else PROJECT_ROOT / "config" / "neo4j_connection.json"
    common = ("--neo4j-config", str(neo4j_config))

    if not args.skip_import:
        importer_args = list(common)
        importer_args += ["--batch-size", str(max(1, min(args.batch_size, 1000)))]
        if args.source:
            importer_args += ["--source", str(args.source.resolve())]
        if args.platform:
            importer_args += [
                "--default-platform", args.platform,
                "--source-id-prefix", args.platform,
            ]
        if args.force_import:
            importer_args.append("--force")
        run_step("1/7 原始数据增量导入", PROJECT_ROOT / "raw_jd_layer" / "importer.py", *importer_args)

    ingest_run_id = ""
    ingestion_report = PROJECT_ROOT / "output" / "raw_jd_ingestion" / "last_run.json"
    if not args.skip_import and ingestion_report.exists():
        ingest_run_id = str(
            json.loads(ingestion_report.read_text(encoding="utf-8")).get("run_id") or ""
        )

    domain_args = [*common, "--batch-size", str(max(1, min(args.batch_size, 500)))]
    if args.limit:
        domain_args += ["--limit", str(args.limit)]
    run_step("2/7 信息技术岗位准入", PROJECT_ROOT / "processing_layer" / "domain_filter.py", *domain_args)

    process_args = [
        *common,
        "--batch-size",
        str(max(1, min(args.processing_batch_size, 200))),
    ]
    if args.limit:
        process_args += ["--limit", str(args.limit)]
    if args.llm_endpoint:
        process_args += ["--llm-endpoint", args.llm_endpoint]
    if args.iflytek_spark:
        process_args.append("--iflytek-spark")
    if ingest_run_id:
        process_args += ["--ingest-run-id", ingest_run_id, "--force"]
    run_step("3/7 仅处理 IT 岗位的能力分析与原文回标", PROJECT_ROOT / "processing_layer" / "processor.py", *process_args)

    if not args.skip_import and ingestion_report.exists():
        ingestion = json.loads(ingestion_report.read_text(encoding="utf-8"))
        processing_report = PROJECT_ROOT / "output" / "jd_processing" / "last_run.json"
        processing = json.loads(processing_report.read_text(encoding="utf-8"))
        imported = int((ingestion.get("metrics") or {}).get("rows_valid") or 0)
        processed_metrics = processing.get("metrics") or {}
        processed = int(processed_metrics.get("rows_read") or 0)
        if imported > 0 and processed == 0:
            raise RuntimeError(
                f"本次已导入 {imported} 条，但能力处理读取 0 条；已禁止继续发布。"
            )
        if int(processed_metrics.get("needs_llm") or 0) > 0:
            raise RuntimeError(
                f"仍有 {processed_metrics['needs_llm']} 条缺少能力分析；"
                "请先补齐五维能力或配置 --llm-endpoint。"
            )
        if int(processed_metrics.get("failed") or 0) > 0:
            raise RuntimeError(
                f"能力处理失败 {processed_metrics['failed']} 条；已禁止继续发布。"
            )

    run_step(
        "4/7 映射到受控岗位分类",
        PROJECT_ROOT / "processing_layer" / "backfill_it_roles.py",
        *common,
        "--batch-size",
        str(max(1, min(args.batch_size, 2000))),
    )

    if args.skip_normalization:
        print("\n已完成原始层和能力证据层；按参数跳过归一化与发布。", flush=True)
        return

    normalize_args = [
        "--work-dir", str(args.work_dir.resolve()),
        "--batch-size", str(max(1, min(args.batch_size, 500))),
        "--neo4j-config", str(neo4j_config),
    ]
    # 新数据需要重新导出当前完整快照；旧目录仅作为可覆盖的中间产物。
    if (args.work_dir / "knowledge_graph.db").exists():
        normalize_args.append("--overwrite")
    if args.limit:
        normalize_args += ["--limit", str(args.limit)]
    run_step("5/7 复用关键词归一化与知识图谱候选生成", PROJECT_ROOT / "processing_layer" / "normalize_with_demo.py", *normalize_args)

    publish_args = [
        "--database", str((args.work_dir / "knowledge_graph.db").resolve()),
        "--normalization-dir", str((args.work_dir / "skill_reports").resolve()),
        "--neo4j-config", str(neo4j_config),
    ]
    if args.publish:
        publish_args.append("--publish")
        label = "6/7 发布到 Neo4j 并切换活动版本"
    else:
        label = "6/7 只读校验（未发布；加 --publish 才写入活动版本）"
    run_step(label, PROJECT_ROOT / "processing_layer" / "publish_normalization.py", *publish_args)

    discovery = {
        "status": "NOT_RUN",
        "reason": "发布活动图谱后才运行新岗位发现",
    }
    discovery_error: subprocess.CalledProcessError | None = None
    if args.publish and args.skip_new_role_discovery:
        discovery = {"status": "SKIPPED", "reason": "--skip-new-role-discovery"}
    elif args.publish:
        discovery_args = [
            "--neo4j-config", str(neo4j_config),
            "--data-root", str(args.new_role_data_root.resolve()),
            "--llm-mode", args.new_role_llm_mode,
            "--role-limit", str(max(0, min(args.new_role_limit, 50))),
            "--skill-limit", str(max(0, min(args.ability_change_limit, 100))),
            "--timeout-seconds", str(max(30, args.new_role_timeout_seconds)),
        ]
        if args.platform:
            discovery_args += ["--skill-source", args.platform]
        try:
            run_module_step(
                "7/7 自动发现新岗位与旧岗位能力变化并生成审核队列",
                "new_role_discovery.demo",
                *discovery_args,
            )
            discovery = {
                "status": "COMPLETED",
                "data_root": str(args.new_role_data_root.resolve()),
                "llm_mode": args.new_role_llm_mode,
                "ability_change_limit": max(0, min(args.ability_change_limit, 100)),
                "ability_change_source": args.platform,
                **latest_discovery_task(args.new_role_data_root.resolve()),
            }
        except subprocess.CalledProcessError as error:
            discovery_error = error
            discovery = {
                "status": "FAILED",
                "exit_code": error.returncode,
                "data_root": str(args.new_role_data_root.resolve()),
                "message": "图谱已发布，但岗位与能力变化发现阶段失败；活动版本未回滚",
            }

    pipeline_report = {
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "FAILED" if discovery_error else "COMPLETED",
        "graph_publish": "COMPLETED" if args.publish else "VALIDATED_ONLY",
        "new_role_discovery": discovery,
    }
    args.work_dir.resolve().mkdir(parents=True, exist_ok=True)
    pipeline_report_path = args.work_dir.resolve() / "incremental_pipeline_report.json"
    pipeline_report_path.write_text(
        json.dumps(pipeline_report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n流水线总报告：{pipeline_report_path}", flush=True)

    if discovery_error:
        raise RuntimeError(
            "知识图谱发布已完成，但岗位与能力变化发现失败；请查看流水线总报告和工作台任务记录"
        ) from discovery_error
    if args.publish and not args.keep_work_db:
        work_database = args.work_dir.resolve() / "knowledge_graph.db"
        if work_database.exists():
            work_database.unlink()
            print(f"\n已清理发布用临时数据库：{work_database}", flush=True)


if __name__ == "__main__":
    main()
