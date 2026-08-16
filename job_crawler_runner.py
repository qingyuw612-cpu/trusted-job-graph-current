"""Scheduler and guarded system handoff for the three recruitment crawlers."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence


PLATFORMS = ("51job", "zhilian", "liepin")
PLATFORM_LABELS = {
    "51job": "前程无忧",
    "zhilian": "智联招聘",
    "liepin": "猎聘",
}


@dataclass(frozen=True)
class RunOptions:
    platforms: tuple[str, ...]
    output_root: Path
    source_dir: Path
    year: int
    pages: int
    city: str | None
    keyword: str | None
    fresh_scan: bool
    non_interactive: bool
    dry_run: bool
    python_executable: str
    reuse_output: bool
    system_import: bool
    system_publish: bool
    pipeline_limit: int
    neo4j_config: Path
    skip_new_role_discovery: bool


def default_source_dir() -> Path:
    configured = os.environ.get("JOB_CRAWLER_SOURCE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(__file__).resolve().parent.parent / "爬虫代码").resolve()


def default_output_root() -> Path:
    return (Path(__file__).resolve().parent.parent / "crawler_standalone_output").resolve()


def platform_python(platform: str, default: str) -> str:
    env_name = f"JOB_CRAWLER_{platform.upper()}_PYTHON"
    return os.environ.get(env_name, default)


def build_command(platform: str, options: RunOptions) -> tuple[list[str], Path]:
    python = platform_python(platform, options.python_executable)
    state_dir = options.output_root / "state" / platform
    state_dir.mkdir(parents=True, exist_ok=True)

    if platform == "51job":
        output = state_dir / "jobs_2026_it.csv"
        command = [
            python,
            str(options.source_dir / "main_51job.py"),
            "--output-dir",
            str(state_dir),
            "--pages",
            str(options.pages),
        ]
        if options.non_interactive:
            command.extend(("--headless", "--skip-login"))
        if options.fresh_scan:
            command.append("--reset-checkpoint")
        if options.keyword:
            command.extend(("--keyword", options.keyword))
        if options.city:
            command.extend(("--city", options.city))
        return command, output

    if platform == "zhilian":
        output = state_dir / f"zhilian_jobs_{options.year}.csv"
        command = [
            python,
            str(options.source_dir / "spider_zhilian_step1.py"),
            "--output",
            str(output),
            "--pages",
            str(options.pages),
            "--year",
            str(options.year),
        ]
        if options.non_interactive:
            command.append("--skip-login")
        if options.fresh_scan:
            command.append("--no-resume")
        if options.keyword:
            command.extend(("--keyword", options.keyword))
        if options.city:
            command.extend(("--city", options.city))
        return command, output

    if platform == "liepin":
        output = state_dir / f"liepin_jobs_{options.year}.csv"
        command = [
            python,
            str(options.source_dir / "liepin_cdp_raw.py"),
            "--target-jobs",
            "--mode",
            "http",
            "--output",
            str(output),
            "--batch-pages",
            str(options.pages),
            "--year",
            str(options.year),
        ]
        if options.fresh_scan:
            command.append("--no-resume")
        if options.keyword:
            command.extend(("--target-keyword", options.keyword))
        if options.city:
            command.extend(("--city", options.city))
        return command, output

    raise ValueError(f"未知平台：{platform}")


def csv_row_count(path: Path) -> int | None:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return sum(1 for _ in csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error):
        return None


def safe_command(command: Sequence[str]) -> list[str]:
    """Return a manifest-safe command (current crawlers have no secrets)."""
    return [str(part) for part in command]


def build_system_ingest_command(
    platform: str, output: Path, options: RunOptions
) -> list[str]:
    command = [
        options.python_executable,
        str(Path(__file__).resolve().parent / "run_incremental_knowledge_graph.py"),
        "--source",
        str(output),
        "--platform",
        PLATFORM_LABELS[platform],
        "--neo4j-config",
        str(options.neo4j_config),
        "--skip-normalization",
        "--iflytek-spark",
        "--force-import",
    ]
    if options.pipeline_limit:
        command.extend(("--limit", str(options.pipeline_limit)))
    return command


def build_system_finalize_command(options: RunOptions, work_dir: Path | None = None) -> list[str]:
    command = [
        options.python_executable,
        str(Path(__file__).resolve().parent / "run_incremental_knowledge_graph.py"),
        "--skip-import",
        "--neo4j-config",
        str(options.neo4j_config),
        "--work-dir",
        str(work_dir or (options.output_root / "system_work")),
    ]
    if options.system_publish:
        command.append("--publish")
    if options.skip_new_role_discovery:
        command.append("--skip-new-role-discovery")
    return command


def run_logged(command: Sequence[str], log_path: Path, cwd: Path) -> tuple[int, float]:
    started = time.monotonic()
    try:
        with log_path.open("w", encoding="utf-8", newline="") as log:
            process = subprocess.run(
                list(command),
                cwd=cwd,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        code = process.returncode
    except OSError as exc:
        code = 127
        log_path.write_text(f"启动失败：{exc}\n", encoding="utf-8")
    return code, round(time.monotonic() - started, 2)


def run_cycle(options: RunOptions) -> tuple[int, Path]:
    started = datetime.now().astimezone()
    cycle_id = started.strftime("%Y%m%d_%H%M%S_%f")
    run_dir = options.output_root / "runs" / cycle_id
    run_dir.mkdir(parents=True, exist_ok=False)
    manifest: dict = {
        "cycle_id": cycle_id,
        "started_at": started.isoformat(timespec="seconds"),
        "mode": "dry-run" if options.dry_run else ("integrated" if options.system_import else "standalone"),
        "system_import_enabled": options.system_import,
        "system_publish_requested": options.system_publish,
        "filters": {
            "year": options.year,
            "pages": options.pages,
            "city": options.city,
            "keyword": options.keyword,
            "fresh_scan": options.fresh_scan,
            "reuse_output": options.reuse_output,
        },
        "platforms": [],
    }
    overall_code = 0

    for platform in options.platforms:
        command, output = build_command(platform, options)
        log_path = run_dir / f"{platform}.log"
        before = csv_row_count(output) or 0
        item = {
            "platform": platform,
            "label": PLATFORM_LABELS[platform],
            "command": safe_command(command),
            "output": str(output),
            "log": str(log_path),
            "rows_before": before,
        }
        if options.reuse_output:
            after = csv_row_count(output)
            status = "reused" if after is not None else "failed"
            item.update(
                status=status,
                return_code=0 if after is not None else 1,
                rows_after=after,
                rows_added=0,
            )
            if after is None:
                overall_code = 1
        elif options.dry_run:
            item.update(status="planned", return_code=None, rows_after=before, rows_added=0)
        else:
            code, duration = run_logged(command, log_path, options.source_dir)
            status = "success" if code == 0 else "failed"
            after = csv_row_count(output)
            item.update(
                status=status,
                return_code=code,
                duration_seconds=duration,
                rows_after=after,
                rows_added=max(0, (after or 0) - before),
            )
            if code != 0:
                overall_code = 1
        manifest["platforms"].append(item)

    if options.system_import:
        project_root = Path(__file__).resolve().parent
        integration: dict = {
            "status": "planned" if options.dry_run else "pending",
            "publish_requested": options.system_publish,
            "neo4j_config": str(options.neo4j_config),
            "imports": [],
        }
        manifest["system_integration"] = integration
        eligible = all(
            item["status"] in {"success", "reused", "planned"}
            for item in manifest["platforms"]
        )
        import_failed = not eligible
        for item in manifest["platforms"]:
            platform = item["platform"]
            output = Path(item["output"])
            command = build_system_ingest_command(platform, output, options)
            log_path = run_dir / f"system_import_{platform}.log"
            step = {
                "platform": platform,
                "label": PLATFORM_LABELS[platform],
                "command": safe_command(command),
                "source": str(output),
                "log": str(log_path),
                "ability_extraction": "iflytek_spark_after_it_domain_filter",
            }
            if options.dry_run:
                step.update(status="planned", return_code=None)
            elif not eligible:
                step.update(status="blocked", return_code=None)
            else:
                code, duration = run_logged(command, log_path, project_root)
                step.update(
                    status="success" if code == 0 else "failed",
                    return_code=code,
                    duration_seconds=duration,
                )
                if code != 0:
                    import_failed = True
                    overall_code = 1
            integration["imports"].append(step)

        finalize_command = build_system_finalize_command(options, run_dir / "system_work")
        finalize_log = run_dir / "system_finalize.log"
        finalize = {
            "command": safe_command(finalize_command),
            "log": str(finalize_log),
            "publishes_active_graph": options.system_publish,
        }
        if options.dry_run:
            finalize.update(status="planned", return_code=None)
            integration["status"] = "planned"
        elif import_failed:
            finalize.update(status="blocked", return_code=None)
            integration["status"] = "blocked"
            overall_code = 1
        else:
            code, duration = run_logged(finalize_command, finalize_log, project_root)
            finalize.update(
                status="success" if code == 0 else "failed",
                return_code=code,
                duration_seconds=duration,
            )
            integration["status"] = "success" if code == 0 else "failed"
            if code != 0:
                overall_code = 1
        integration["finalize"] = finalize

    finished = datetime.now().astimezone()
    manifest["finished_at"] = finished.isoformat(timespec="seconds")
    manifest["status"] = "planned" if options.dry_run else ("success" if overall_code == 0 else "partial_failure")
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return overall_code, manifest_path


def acquire_lock(output_root: Path) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    lock_path = output_root / "crawler.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError(f"已有采集任务在运行；锁文件：{lock_path}") from exc
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(str(os.getpid()))
    return lock_path


def options_from_args(args: argparse.Namespace) -> RunOptions:
    platforms = tuple(args.platform or PLATFORMS)
    return RunOptions(
        platforms=platforms,
        output_root=Path(args.output_root).expanduser().resolve(),
        source_dir=Path(args.source_dir).expanduser().resolve(),
        year=args.year,
        pages=args.pages,
        city=args.city,
        keyword=args.keyword,
        fresh_scan=args.fresh_scan,
        non_interactive=args.non_interactive,
        dry_run=args.dry_run,
        python_executable=args.python,
        reuse_output=args.reuse_output,
        system_import=args.system_import,
        system_publish=args.system_publish,
        pipeline_limit=args.pipeline_limit,
        neo4j_config=Path(args.neo4j_config).expanduser().resolve(),
        skip_new_role_discovery=args.skip_new_role_discovery,
    )


def validate(options: RunOptions) -> None:
    if not 2000 <= options.year <= 2100:
        raise ValueError("--year 必须在 2000 到 2100 之间")
    if not 1 <= options.pages <= 20:
        raise ValueError("--pages 必须在 1 到 20 之间")
    if options.system_publish and not options.system_import:
        raise ValueError("--system-publish 必须与 --system-import 一起使用")
    if options.reuse_output and not options.system_import:
        raise ValueError("--reuse-output 仅用于把已有采集结果接入系统")
    if options.pipeline_limit < 0:
        raise ValueError("--pipeline-limit 不能小于 0")
    if options.system_import and not options.neo4j_config.is_file():
        raise FileNotFoundError(f"Neo4j 配置不存在：{options.neo4j_config}")
    missing = [str(options.source_dir / name) for name in (
        "main_51job.py", "spider_zhilian_step1.py", "liepin_cdp_raw.py"
    ) if not (options.source_dir / name).is_file()]
    if missing:
        raise FileNotFoundError("缺少爬虫文件：" + "、".join(missing))


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--platform", action="append", choices=PLATFORMS, help="可重复；默认三个平台")
    parser.add_argument("--source-dir", default=str(default_source_dir()), help="三个原始爬虫所在目录")
    parser.add_argument("--output-root", default=str(default_output_root()), help="独立采集状态和运行报告目录")
    parser.add_argument("--year", type=int, default=datetime.now().year)
    parser.add_argument("--pages", type=int, default=20, help="每岗位、城市最多页数，1-20")
    parser.add_argument("--city", choices=("北京", "上海", "广州", "深圳"))
    parser.add_argument("--keyword", help="仅试跑一个内置岗位关键词")
    parser.add_argument("--fresh-scan", action=argparse.BooleanOptionalAction, default=False, help="忽略已完成页重新扫描，职位仍去重")
    parser.add_argument("--non-interactive", action="store_true", help="不等待 51job/智联手工登录；猎聘仍需已有登录态")
    parser.add_argument("--dry-run", action="store_true", help="只展示计划，不启动爬虫")
    parser.add_argument("--python", default=sys.executable, help="默认 Python；可用 JOB_CRAWLER_<平台>_PYTHON 单独覆盖")
    parser.add_argument("--reuse-output", action="store_true", help="不重新爬取，直接接入各平台已有 CSV")
    parser.add_argument("--system-import", action="store_true", help="采集成功后接入原始审计层并运行图谱处理")
    parser.add_argument("--system-publish", action="store_true", help="处理成功后发布并切换活动图谱版本")
    parser.add_argument("--pipeline-limit", type=int, default=0, help="每个平台最多处理 N 条；0 表示全部")
    parser.add_argument(
        "--neo4j-config",
        default=str(Path(__file__).resolve().parent / "config" / "neo4j_connection.json"),
    )
    parser.add_argument("--skip-new-role-discovery", action="store_true", help="发布后跳过岗位与能力变化发现")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="三个招聘平台的定时采集与知识图谱增量接入器")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="立即执行一个采集周期")
    add_common_arguments(run)
    schedule = subparsers.add_parser("schedule", help="常驻进程按固定间隔采集")
    add_common_arguments(schedule)
    schedule.add_argument("--interval-minutes", type=float, required=True, help="两次采集开始时间的最小间隔")
    schedule.set_defaults(fresh_scan=True, non_interactive=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    options = options_from_args(args)
    try:
        validate(options)
        lock_path = acquire_lock(options.output_root)
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 2
    try:
        if args.command == "run":
            return run_cycle(options)[0]
        if args.interval_minutes <= 0:
            print("启动失败：--interval-minutes 必须大于 0", file=sys.stderr)
            return 2
        interval_seconds = args.interval_minutes * 60
        while True:
            cycle_started = time.monotonic()
            run_cycle(options)
            wait_seconds = max(0.0, interval_seconds - (time.monotonic() - cycle_started))
            print(f"下一轮将在 {wait_seconds / 60:.1f} 分钟后开始。按 Ctrl+C 停止。", flush=True)
            time.sleep(wait_seconds)
    except KeyboardInterrupt:
        print("已停止定时采集。")
        return 0
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
