"""Scheduler and guarded system handoff for the three recruitment crawlers."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from collections import deque
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
KEYWORD_CONFIG = Path(__file__).resolve().parent / "config" / "job_radar_keywords.json"


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
    scan_mode: str = "legacy"
    collection_limit: int = 0
    allow_processing_failures: bool = False
    new_role_limit: int = 50
    ability_change_limit: int = 40
    fast_demo: bool = False
    sample_limit: int = 0


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


def build_command(
    platform: str, options: RunOptions, keyword: str | None = None
) -> tuple[list[str], Path]:
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
        selected_keyword = keyword if keyword is not None else options.keyword
        if selected_keyword:
            command.extend(("--keyword", selected_keyword))
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
        selected_keyword = keyword if keyword is not None else options.keyword
        if selected_keyword:
            command.extend(("--keyword", selected_keyword))
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
        selected_keyword = keyword if keyword is not None else options.keyword
        if selected_keyword:
            command.extend(("--target-keyword", selected_keyword))
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


def liepin_login_ready(options: RunOptions) -> bool:
    """Check the loopback-only server browser without exposing its cookies."""
    command = [
        platform_python("liepin", options.python_executable),
        str(options.source_dir / "liepin_cdp_raw.py"),
        "--login-status",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=str(options.source_dir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def write_live_status(output_root: Path, payload: dict) -> Path:
    """Atomically publish a small, public-safe maintenance progress snapshot."""
    output_root.mkdir(parents=True, exist_ok=True)
    status_path = output_root / "current_status.json"
    previous: dict = {}
    try:
        loaded = json.loads(status_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            previous = loaded
    except (OSError, json.JSONDecodeError):
        pass
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    publish_status = str(
        (payload.get("graph_updates") or {}).get("publish_status") or ""
    ).lower()
    terminal_success = (
        payload.get("phase") == "complete"
        and (
            payload.get("status") == "success"
            or (
                payload.get("status") == "partial_failure"
                and publish_status in {"success", "completed", "skipped"}
            )
        )
    )
    snapshot = {
        "schema_version": 2,
        "last_success_at": now if terminal_success else str(previous.get("last_success_at") or ""),
        "last_success_cycle_id": (
            str(payload.get("cycle_id") or "")
            if terminal_success
            else str(previous.get("last_success_cycle_id") or "")
        ),
        **payload,
        "updated_at": now,
    }
    temporary = output_root / f".current_status.{os.getpid()}.tmp"
    temporary.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(status_path)
    return status_path


def load_keyword_config(path: Path = KEYWORD_CONFIG) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    full = tuple(str(item).strip() for item in data.get("full_keywords", []) if str(item).strip())
    quick = tuple(str(item).strip() for item in data.get("quick_keywords", []) if str(item).strip())
    if not full or not quick or not set(quick).issubset(full):
        raise ValueError("岗位雷达关键词配置无效")
    return {**data, "full_keywords": full, "quick_keywords": quick}


def scan_keywords(options: RunOptions) -> tuple[str | None, ...]:
    if options.scan_mode == "legacy":
        return (options.keyword,)
    config = load_keyword_config()
    if options.scan_mode == "target":
        if not options.keyword or options.keyword not in config["full_keywords"]:
            raise ValueError("指定岗位必须来自统一关键词池")
        return (options.keyword,)
    key = "quick_keywords" if options.scan_mode == "quick" else "full_keywords"
    return tuple(config[key])


def write_capped_csv(source: Path, target: Path, limit: int) -> int:
    """Write at most the newest ``limit`` rows for guarded system import."""
    limit = max(0, limit)
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = deque(reader, maxlen=limit)
    if not fieldnames:
        raise ValueError(f"采集结果缺少 CSV 表头：{source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


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
    if options.allow_processing_failures:
        command.append("--allow-processing-failures")
    return command


def build_system_finalize_command(
    options: RunOptions,
    work_dir: Path | None = None,
    ingest_run_ids: Sequence[str] = (),
) -> list[str]:
    evolution_data_root = os.environ.get("EVOLUTION_DATA_ROOT", "").strip()
    if options.fast_demo:
        command = [
            options.python_executable,
            "-u",
            "-m",
            "new_role_discovery.demo",
            "--neo4j-config",
            str(options.neo4j_config),
            "--data-root",
            str(Path(__file__).resolve().parent / "output" / "role_evolution_workbench_v2"),
            "--llm-mode",
            "auto",
            "--role-limit",
            str(max(0, min(options.new_role_limit, 50))),
            "--skill-limit",
            str(max(0, min(options.ability_change_limit, 100))),
            "--sample-limit",
            str(max(0, min(options.sample_limit, 20000))),
            "--timeout-seconds",
            "900",
        ]
        if evolution_data_root:
            command[command.index("--data-root") + 1] = evolution_data_root
        return command
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
    for ingest_run_id in dict.fromkeys(value for value in ingest_run_ids if value):
        command.extend(("--ingest-run-id", ingest_run_id))
    if options.skip_new_role_discovery:
        command.append("--skip-new-role-discovery")
    command.extend(("--new-role-limit", str(max(0, min(options.new_role_limit, 50)))))
    command.extend(("--ability-change-limit", str(max(0, min(options.ability_change_limit, 100)))))
    if evolution_data_root:
        command.extend(("--new-role-data-root", evolution_data_root))
    return command


def run_logged(
    command: Sequence[str], log_path: Path, cwd: Path, *, append: bool = False
) -> tuple[int, float]:
    started = time.monotonic()
    try:
        # Windows often gives child Python processes a cp936/GBK stdout even
        # though the log file is opened as UTF-8.  The crawler output contains
        # characters such as ``✅`` and non-breaking hyphens, so let the child
        # encode its own console output as UTF-8 before redirecting it here.
        child_env = os.environ.copy()
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUTF8"] = "1"
        with log_path.open("a" if append else "w", encoding="utf-8", newline="") as log:
            process = subprocess.run(
                list(command),
                cwd=cwd,
                env=child_env,
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
        "mode": "dry-run" if options.dry_run else (
            "integrated-fast-demo" if options.fast_demo else ("integrated" if options.system_import else "standalone")
        ),
        "system_import_enabled": options.system_import,
        "system_publish_requested": options.system_publish,
        "filters": {
            "year": options.year,
            "pages": options.pages,
            "city": options.city,
            "keyword": options.keyword,
            "fresh_scan": options.fresh_scan,
            "reuse_output": options.reuse_output,
            "scan_mode": options.scan_mode,
            "collection_limit": options.collection_limit,
        },
        "platforms": [],
    }
    overall_code = 0

    keywords = scan_keywords(options)

    def publish_status(
        status: str,
        phase: str,
        message: str,
        *,
        current_platform: str = "",
        keyword_index: int = 0,
        keyword_total: int = 0,
    ) -> None:
        platform_items = [
            {
                "platform": item["platform"],
                "label": item["label"],
                "status": item.get("status", "pending"),
                "keywords_scanned": int(item.get("keywords_scanned") or 0),
                "keywords_planned": int(item.get("keywords_planned") or 0),
                "rows_added": int(item.get("rows_added") or 0),
                "rows_selected_for_import": int(item.get("rows_selected_for_import") or 0),
            }
            for item in manifest["platforms"]
        ]
        integration = manifest.get("system_integration") or {}
        imports = integration.get("imports") or []
        finished_imports = sum(
            1
            for item in imports
            if item.get("status") in {"success", "failed", "skipped", "blocked"}
        )
        scanned = sum(item["keywords_scanned"] for item in platform_items)
        total_units = max(
            1,
            len(options.platforms) * len(keywords) + len(options.platforms) + 1,
        )
        completed_units = scanned + finished_imports
        if (integration.get("finalize") or {}).get("status") in {
            "success", "failed", "skipped", "blocked"
        }:
            completed_units += 1
        if status in {"success", "partial_failure", "planned"}:
            completed_units = total_units
        write_live_status(
            options.output_root,
            {
                "status": status,
                "phase": phase,
                "message": message,
                "cycle_id": cycle_id,
                "started_at": manifest["started_at"],
                "finished_at": manifest.get("finished_at", ""),
                "current_platform": current_platform,
                "current_platform_label": PLATFORM_LABELS.get(current_platform, ""),
                "keyword_index": keyword_index,
                "keyword_total": keyword_total,
                "progress_percent": round(
                    min(1.0, completed_units / total_units) * 100, 1
                ),
                "platforms": platform_items,
                "totals": {
                    "rows_added": sum(item["rows_added"] for item in platform_items),
                    "rows_selected_for_import": sum(
                        item["rows_selected_for_import"] for item in platform_items
                    ),
                    "platforms_completed": sum(
                        1
                        for item in platform_items
                        if item["status"] in {"success", "reused"}
                    ),
                    "platforms_total": len(options.platforms),
                },
                "graph_updates": manifest.get(
                    "graph_updates",
                    {
                        "new_role_candidates": None,
                        "updated_roles": None,
                        "updated_skill_edges": None,
                        "publish_status": "pending",
                    },
                ),
                "schedule": {
                    "interval_hours": 12,
                    "random_delay_minutes": 15,
                },
            },
        )

    publish_status("running", "collect", "正在准备本轮数据采集")
    for platform in options.platforms:
        command, output = build_command(platform, options, keywords[0])
        log_path = run_dir / f"{platform}.log"
        before = csv_row_count(output) or 0
        item = {
            "platform": platform,
            "label": PLATFORM_LABELS[platform],
            "command": safe_command(command),
            "output": str(output),
            "log": str(log_path),
            "rows_before": before,
            "scan_mode": options.scan_mode,
            "keywords_planned": len(keywords),
            "keywords_scanned": 0,
        }
        manifest["platforms"].append(item)
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
        elif platform == "liepin" and options.non_interactive and not liepin_login_ready(options):
            # Liepin's batch collector requires an interactive account session
            # for complete job descriptions. A systemd job must not launch a
            # browser and wait for a person, so defer this source explicitly
            # while the other sources continue through the graph pipeline.
            item.update(
                status="skipped_auth",
                return_code=None,
                rows_after=before,
                rows_added=0,
                skip_reason="interactive_login_required",
            )
        else:
            code = 0
            duration = 0.0
            for index, selected_keyword in enumerate(keywords):
                item["status"] = "running"
                publish_status(
                    "running",
                    "collect",
                    f"正在采集{PLATFORM_LABELS[platform]}数据",
                    current_platform=platform,
                    keyword_index=index + 1,
                    keyword_total=len(keywords),
                )
                keyword_command, output = build_command(platform, options, selected_keyword)
                item["command"] = safe_command(keyword_command)
                step_code, step_duration = run_logged(
                    keyword_command,
                    log_path,
                    options.source_dir,
                    append=index > 0,
                )
                duration += step_duration
                item["keywords_scanned"] = index + 1
                item["rows_after"] = csv_row_count(output)
                item["rows_added"] = max(0, (item["rows_after"] or 0) - before)
                publish_status(
                    "running",
                    "collect",
                    f"已完成{PLATFORM_LABELS[platform]}第 {index + 1} 个采集方向",
                    current_platform=platform,
                    keyword_index=index + 1,
                    keyword_total=len(keywords),
                )
                if step_code != 0:
                    code = step_code
                    break
                current_rows = csv_row_count(output) or before
                if options.collection_limit and current_rows - before >= options.collection_limit:
                    item["stopped_at_collection_limit"] = True
                    break
            after = csv_row_count(output)
            status = "success" if code == 0 else "failed"
            rows_added = max(0, (after or 0) - before)
            item.update(
                status=status,
                return_code=code,
                duration_seconds=duration,
                rows_after=after,
                rows_added=rows_added,
            )
            if code != 0:
                overall_code = 1
            if code == 0 and options.collection_limit and output.is_file():
                capped = run_dir / f"{platform}_capped.csv"
                item["integration_source"] = str(capped)
                item["rows_selected_for_import"] = write_capped_csv(
                    output, capped, min(rows_added, options.collection_limit)
                )
        publish_status(
            "running",
            "collect",
            (
                f"{PLATFORM_LABELS[platform]}需要人工登录，自动任务已跳过"
                if item.get("status") == "skipped_auth"
                else (
                    f"{PLATFORM_LABELS[platform]}采集完成"
                    if item.get("status") != "failed"
                    else f"{PLATFORM_LABELS[platform]}采集失败"
                )
            ),
            current_platform=platform,
            keyword_index=int(item.get("keywords_scanned") or 0),
            keyword_total=len(keywords),
        )

    if options.system_import:
        project_root = Path(__file__).resolve().parent
        integration: dict = {
            "status": "planned" if options.dry_run else "pending",
            "publish_requested": options.system_publish and not options.fast_demo,
            "neo4j_config": str(options.neo4j_config),
            "imports": [],
            "warnings": [],
        }
        manifest["system_integration"] = integration
        eligible_platforms = {
            item["platform"]
            for item in manifest["platforms"]
            if item["status"] in {"success", "reused", "planned"}
        }
        import_failed = not eligible_platforms
        ingest_run_ids: list[str] = []
        for item in manifest["platforms"]:
            platform = item["platform"]
            output = Path(item.get("integration_source") or item["output"])
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
            integration["imports"].append(step)
            step["status"] = "running"
            publish_status(
                "running",
                "clean_import",
                f"正在清洗并导入{PLATFORM_LABELS[platform]}数据",
                current_platform=platform,
            )
            no_new_rows = (
                not options.reuse_output
                and int(item.get("rows_selected_for_import") or 0) == 0
            )
            missing_demo_llm = (
                options.fast_demo
                and not os.environ.get("IFLYTEK_SPARK_API_PASSWORD", "").strip()
            )
            if options.dry_run:
                step.update(status="planned", return_code=None)
            elif platform not in eligible_platforms:
                step.update(
                    status="blocked",
                    return_code=None,
                    reason=(
                        "interactive_login_required"
                        if item.get("status") == "skipped_auth"
                        else "platform_collection_failed"
                    ),
                )
            elif no_new_rows:
                step.update(
                    status="skipped",
                    return_code=0,
                    reason="本轮没有新增 JD，复用已有图谱进行快速分析",
                )
            elif missing_demo_llm:
                warning = (
                    "服务器未配置 IFLYTEK_SPARK_API_PASSWORD；本轮已保留采集结果，"
                    "跳过新增 JD 的能力入图并复用已有图谱完成快速分析"
                )
                step.update(
                    status="skipped",
                    return_code=0,
                    reason="missing_iflytek_credentials",
                )
                integration["warnings"].append(warning)
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
                else:
                    ingestion_report = (
                        project_root / "output" / "raw_jd_ingestion" / "last_run.json"
                    )
                    try:
                        ingestion = json.loads(ingestion_report.read_text(encoding="utf-8"))
                        ingest_run_id = str(ingestion.get("run_id") or "")
                    except (OSError, json.JSONDecodeError):
                        ingest_run_id = ""
                    if ingest_run_id:
                        step["ingest_run_id"] = ingest_run_id
                        ingest_run_ids.append(ingest_run_id)
            publish_status(
                "running",
                "clean_import",
                f"{PLATFORM_LABELS[platform]}清洗导入步骤已结束",
                current_platform=platform,
            )

        finalize_command = build_system_finalize_command(
            options, run_dir / "system_work", ingest_run_ids
        )
        finalize_log = run_dir / "system_finalize.log"
        finalize = {
            "command": safe_command(finalize_command),
            "log": str(finalize_log),
            "publishes_active_graph": options.system_publish and not options.fast_demo,
            "fast_demo": options.fast_demo,
        }
        integration["finalize"] = finalize
        finalize["status"] = "running"
        publish_status(
            "running",
            "normalize_publish",
            "正在进行岗位与技能归一化并发布知识图谱",
        )
        if options.dry_run:
            finalize.update(status="planned", return_code=None)
            integration["status"] = "planned"
        elif import_failed:
            finalize.update(status="blocked", return_code=None)
            integration["status"] = "blocked"
            overall_code = 1
        elif not ingest_run_ids and all(
            step.get("status") == "skipped" for step in integration["imports"]
        ):
            finalize.update(
                status="skipped",
                return_code=0,
                reason="本轮没有可发布的新增数据，保留当前活动图谱",
            )
            integration["status"] = "success"
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
        pipeline_report_path = (
            run_dir / "system_work" / "incremental_pipeline_report.json"
        )
        try:
            pipeline_report = json.loads(
                pipeline_report_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            pipeline_report = {}
        normalization = pipeline_report.get("normalization") or {}
        discovery_result = pipeline_report.get("new_role_discovery") or {}
        discovery_summary = discovery_result.get("summary") or {}
        manifest["graph_updates"] = {
            "new_role_candidates": discovery_summary.get("new_role_candidates"),
            "public_new_role_candidates": discovery_summary.get(
                "public_new_role_candidates"
            ),
            "ability_change_candidates": discovery_summary.get(
                "skill_change_candidates"
            ),
            "ability_change_roles": discovery_summary.get("skill_review_roles"),
            "evolution_task_id": discovery_result.get("task_id"),
            "evolution_pipeline_contract": discovery_result.get(
                "pipeline_contract_version"
            ),
            "updated_roles": len(normalization.get("affected_roles") or []),
            "updated_skill_edges": normalization.get("updated_core_edges"),
            "publish_status": pipeline_report.get("graph_publish") or (
                "skipped" if finalize.get("status") == "skipped" else finalize.get("status")
            ),
        }
        publish_status(
            "running",
            "normalize_publish",
            "岗位归一化与图谱发布步骤已结束",
        )

    finished = datetime.now().astimezone()
    manifest["finished_at"] = finished.isoformat(timespec="seconds")
    manifest["status"] = "planned" if options.dry_run else ("success" if overall_code == 0 else "partial_failure")
    publish_status(
        manifest["status"],
        "complete",
        "本轮维护已完成" if overall_code == 0 else "本轮维护部分完成，成功来源已更新知识图谱",
    )
    service_code = overall_code
    integration = manifest.get("system_integration") or {}
    if integration.get("status") == "success" and any(
        item.get("status") in {"success", "reused"}
        for item in manifest["platforms"]
    ):
        service_code = 0
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return service_code, manifest_path


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def acquire_lock(output_root: Path) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    lock_path = output_root / "crawler.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        try:
            owner_pid = int(lock_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            owner_pid = -1
        if _pid_is_running(owner_pid):
            raise RuntimeError(f"已有采集任务在运行；锁文件：{lock_path}") from exc
        try:
            lock_path.unlink()
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileExistsError, OSError) as retry_error:
            raise RuntimeError(f"无法回收失效采集锁；锁文件：{lock_path}") from retry_error
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
        scan_mode=args.scan_mode,
        collection_limit=args.collection_limit,
        allow_processing_failures=args.allow_processing_failures,
        new_role_limit=args.new_role_limit,
        ability_change_limit=args.ability_change_limit,
        fast_demo=args.fast_demo,
        sample_limit=args.sample_limit,
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
    if options.scan_mode not in {"legacy", "full", "quick", "target"}:
        raise ValueError("--scan-mode 不受支持")
    if options.collection_limit < 0:
        raise ValueError("--collection-limit 不能小于 0")
    if options.sample_limit < 0 or options.sample_limit > 20000:
        raise ValueError("--sample-limit 必须在 0 到 20000 之间")
    scan_keywords(options)
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
    parser.add_argument(
        "--scan-mode",
        choices=("legacy", "full", "quick", "target"),
        default="legacy",
        help="关键词巡检模式；legacy 保持旧行为，full/quick 使用统一关键词池",
    )
    parser.add_argument(
        "--collection-limit",
        type=int,
        default=0,
        help="本轮每个平台新增 JD 达到该数量后停止继续遍历关键词；0 表示不限",
    )
    parser.add_argument("--fresh-scan", action=argparse.BooleanOptionalAction, default=False, help="忽略已完成页重新扫描，职位仍去重")
    parser.add_argument("--non-interactive", action="store_true", help="不等待 51job/智联手工登录；猎聘仍需已有登录态")
    parser.add_argument("--dry-run", action="store_true", help="只展示计划，不启动爬虫")
    parser.add_argument("--python", default=sys.executable, help="默认 Python；可用 JOB_CRAWLER_<平台>_PYTHON 单独覆盖")
    parser.add_argument("--reuse-output", action="store_true", help="不重新爬取，直接接入各平台已有 CSV")
    parser.add_argument("--system-import", action="store_true", help="采集成功后接入原始审计层并运行图谱处理")
    parser.add_argument("--system-publish", action="store_true", help="处理成功后发布并切换活动图谱版本")
    parser.add_argument("--pipeline-limit", type=int, default=0, help="每个平台最多处理 N 条；0 表示全部")
    parser.add_argument(
        "--allow-processing-failures",
        action="store_true",
        help="允许少量单条能力分析失败后继续发布，适合小样本演示",
    )
    parser.add_argument("--new-role-limit", type=int, default=50, help="新岗位 Lite 复核上限")
    parser.add_argument("--ability-change-limit", type=int, default=40, help="能力变化 Lite 复核上限")
    parser.add_argument("--sample-limit", type=int, default=0, help="新岗位发现最多读取最近 N 条 IT JD；0 表示全量")
    parser.add_argument(
        "--fast-demo",
        action="store_true",
        help="演示模式：入图后直接做新岗位发现，跳过全量归一化和活动图谱切换",
    )
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
