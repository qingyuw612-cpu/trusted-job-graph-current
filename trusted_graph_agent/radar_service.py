"""Bounded crawler-to-graph jobs exposed by the local graph API."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any


PLATFORMS = {"51job": "前程无忧", "zhilian": "智联招聘", "liepin": "猎聘"}
CITIES = {"", "北京", "上海", "广州", "深圳"}


class RadarBusyError(RuntimeError):
    """Raised when a second radar run is requested while one is active."""


class RadarRunManager:
    """Run one guarded crawler pipeline at a time and expose observable state."""

    def __init__(
        self,
        project_root: Path,
        neo4j_config: Path,
        *,
        python_executable: str | None = None,
        source_dir: Path | None = None,
        output_root: Path | None = None,
        evolution_root: Path | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.neo4j_config = neo4j_config.resolve()
        self.python_executable = python_executable or sys.executable
        workspace_root = self.project_root.parent
        self.source_dir = (source_dir or workspace_root / "爬虫代码").resolve()
        self.output_root = (output_root or workspace_root / "crawler_standalone_output").resolve()
        configured_evolution_root = evolution_root or os.environ.get("EVOLUTION_DATA_ROOT")
        self.evolution_root = Path(configured_evolution_root).resolve() if configured_evolution_root else (
            self.project_root / "output" / "role_evolution_workbench_v2"
        ).resolve()
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._state: dict[str, Any] = self._idle_state()

    @staticmethod
    def _idle_state() -> dict[str, Any]:
        return {
            "status": "idle",
            "stage": "standby",
            "progress": 0,
            "message": "雷达在线，等待启动监测",
            "run_id": "",
            "request": {},
            "result": {},
            "started_at": "",
            "finished_at": "",
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._state)

    def configuration(self) -> dict[str, Any]:
        path = self.project_root / "config" / "job_radar_keywords.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            "version": data.get("version", ""),
            "cities": data.get("cities", []),
            "full_keywords": data.get("full_keywords", []),
            "quick_keywords": data.get("quick_keywords", []),
            "platforms": [{"id": "all", "label": "三平台联合"}]
            + [{"id": key, "label": label} for key, label in PLATFORMS.items()],
        }

    def latest_discovery_result(self) -> dict[str, Any]:
        """Return a small, frontend-safe view of the latest completed discovery."""
        task, status = self._latest_evolution_task()
        if task is None:
            return {"status": "empty", "task_id": "", "candidates": [], "ability_changes": []}
        if status != "ready":
            return {
                "status": status,
                "task_id": str(task.get("task_id") or ""),
                "completed_at": str(task.get("completed_at") or ""),
                "candidates": [],
                "ability_changes": [],
            }
        output_dir = self._safe_output_dir(task)
        if output_dir is None:
            return {"status": "empty", "task_id": "", "candidates": [], "ability_changes": []}
        candidates = self._read_json_list(output_dir / "new_role_candidates.json")
        changes = self._read_json_list(output_dir / "role_skill_changes.json")
        return {
            "status": "ready",
            "task_id": str(task.get("task_id") or ""),
            "completed_at": str(task.get("completed_at") or ""),
            "summary": task.get("summary") or {},
            "candidates": [self._public_candidate(row) for row in candidates[:50]],
            "ability_changes": [self._public_change(row) for row in changes[:100]],
        }

    def latest_evolution_result(self) -> dict[str, Any]:
        """Return the latest role-skill evolution artifact without new-role data."""
        task, status = self._latest_evolution_task()
        if task is None:
            return self._evolution_response("empty")
        response = self._evolution_response(status, task)
        if status != "ready":
            return response
        output_dir = self._safe_output_dir(task)
        if output_dir is None:
            response["status"] = "failed"
            response["error"] = "invalid_output_dir"
            return response
        changes_path = output_dir / "role_skill_changes.json"
        if not changes_path.is_file():
            response["status"] = "failed"
            response["error"] = "missing_role_skill_changes"
            return response
        changes = self._read_json_list(changes_path)
        response["changes"] = [self._public_change(row) for row in changes[:500]]
        return response

    def _latest_evolution_task(self) -> tuple[dict[str, Any] | None, str]:
        jobs_path = self.evolution_root / "role_evolution_jobs" / "jobs.json"
        try:
            payload = json.loads(jobs_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return None, "empty"
        if isinstance(payload, dict):
            tasks = payload.get("tasks") or payload.get("jobs") or []
        else:
            tasks = payload
        if not isinstance(tasks, list):
            return None, "empty"
        tasks = [task for task in tasks if isinstance(task, dict)]
        if not tasks:
            return None, "empty"
        task = max(
            tasks,
            key=lambda row: str(
                row.get("completed_at")
                or row.get("updated_at")
                or row.get("started_at")
                or row.get("created_at")
                or ""
            ),
        )
        raw_status = str(task.get("status") or "").upper()
        if raw_status in {"QUEUED", "RUNNING", "IN_PROGRESS"}:
            return task, "running"
        if raw_status in {"FAILED", "ERROR", "INTERRUPTED"}:
            return task, "failed"
        if raw_status in {"REVIEW_READY", "READY", "COMPLETED", "SUCCESS"}:
            return task, "ready"
        return task, "empty"

    def _evolution_response(self, status: str, task: dict[str, Any] | None = None) -> dict[str, Any]:
        task = task or {}
        summary = task.get("summary") if isinstance(task.get("summary"), dict) else {}
        parameters = task.get("parameters") if isinstance(task.get("parameters"), dict) else {}
        window = {
            key: value
            for key, value in {
                "baseline_start": summary.get("baseline_start") or parameters.get("baseline_start"),
                "cutoff": summary.get("cutoff") or parameters.get("cutoff"),
                "as_of": summary.get("as_of") or parameters.get("as_of"),
            }.items()
            if value not in (None, "")
        }
        return {
            "status": status,
            "task_id": str(task.get("task_id") or ""),
            "completed_at": str(task.get("completed_at") or ""),
            "observation_window": window,
            "summary": summary,
            "changes": [],
        }

    def _safe_output_dir(self, task: dict[str, Any]) -> Path | None:
        raw_output_dir = str(task.get("output_dir") or "").strip()
        if not raw_output_dir:
            return None
        output_dir = Path(raw_output_dir).resolve()
        allowed_root = (self.evolution_root / "role_evolution_runs").resolve()
        try:
            output_dir.relative_to(allowed_root)
        except ValueError:
            return None
        return output_dir if output_dir.is_dir() else None

    @staticmethod
    def _read_json_list(path: Path) -> list[dict[str, Any]]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []

    @staticmethod
    def _public_candidate(row: dict[str, Any]) -> dict[str, Any]:
        semantic = row.get("semantic_review") or {}
        required = row.get("required_skill_draft") or []
        bonus = row.get("bonus_skill_draft") or []
        industries = row.get("typical_industries") or []
        return {
            "id": str(row.get("candidate_id") or ""),
            "name": str(row.get("candidate_title") or "未命名候选岗位"),
            "score": float(row.get("emergence_score") or 0),
            "jobs": int(row.get("current_jd_count") or 0),
            "companies": int(row.get("current_company_count") or 0),
            "templates": int(row.get("current_template_count") or 0),
            "sources": int(row.get("independent_source_count") or 0),
            "growth_windows": int(row.get("growth_windows") or 0),
            "status": "待审核" if row.get("rule_state") == "REVIEW" else "观察中",
            "summary": str(semantic.get("summary") or "由近期多企业招聘信号聚合形成的新岗位候选。"),
            "required": [str(item.get("skill") or item.get("name") or "") for item in required if isinstance(item, dict)][:8],
            "bonus": [str(item.get("skill") or item.get("name") or "") for item in bonus if isinstance(item, dict)][:8],
            "industries": [str(item.get("industry") or "") for item in industries if isinstance(item, dict)][:5],
        }

    @staticmethod
    def _public_change(row: dict[str, Any]) -> dict[str, Any]:
        def optional_number(key: str, integer: bool = False) -> int | float | None:
            value = row.get(key)
            if value in (None, ""):
                return None
            try:
                return int(value) if integer else float(value)
            except (TypeError, ValueError):
                return None

        return {
            "role": str(row.get("role") or ""),
            "skill": str(row.get("skill") or ""),
            "change_type": str(row.get("change_type") or ""),
            "baseline_coverage": optional_number("baseline_coverage"),
            "current_coverage": optional_number("current_coverage"),
            "delta": optional_number("delta"),
            "baseline_company_count": optional_number("baseline_company_count", integer=True),
            "current_company_count": optional_number("current_company_count", integer=True),
            "rule_state": row.get("rule_state"),
            "confidence": optional_number("confidence"),
        }

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = self.validate_request(payload)
        with self._lock:
            if self._state["status"] in {"queued", "running"}:
                raise RadarBusyError("已有雷达任务正在运行，请等待本轮完成")
            run_id = datetime.now().astimezone().strftime("radar_%Y%m%d_%H%M%S_%f")
            self._state = {
                "status": "queued",
                "stage": "queued",
                "progress": 2,
                "message": "任务已进入采集队列",
                "run_id": run_id,
                "request": request,
                "result": {},
                "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "finished_at": "",
            }
        thread = threading.Thread(target=self._run, args=(run_id, request), daemon=True)
        thread.start()
        return self.status()

    def validate_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        platform = str(payload.get("platform") or "all").strip().lower()
        if platform not in {*PLATFORMS, "all"}:
            raise ValueError("platform 仅支持 all、51job、zhilian、liepin")
        scan_mode = str(payload.get("scan_mode") or "full").strip().lower()
        if scan_mode not in {"full", "quick", "target"}:
            raise ValueError("scan_mode 仅支持 full、quick、target")
        try:
            limit = int(payload.get("limit", 300))
            pages = int(payload.get("pages", 1))
        except (TypeError, ValueError) as exc:
            raise ValueError("limit 和 pages 必须是整数") from exc
        if not 20 <= limit <= 2000:
            raise ValueError("limit 必须在 20 到 2000 之间")
        if not 1 <= pages <= 3:
            raise ValueError("pages 必须在 1 到 3 之间")
        city = str(payload.get("city") or "").strip()
        if city not in CITIES:
            raise ValueError("city 仅支持北京、上海、广州、深圳或留空")
        keyword = str(payload.get("keyword") or "").strip()
        if len(keyword) > 40:
            raise ValueError("keyword 最多 40 个字符")
        config = self.configuration()
        full_keywords = tuple(config.get("full_keywords") or [])
        if scan_mode == "target" and keyword not in full_keywords:
            raise ValueError("指定岗位必须从统一关键词池中选择")
        if scan_mode != "target":
            keyword = ""
        platforms = tuple(PLATFORMS) if platform == "all" else (platform,)
        limit_per_platform = max(1, limit // len(platforms))
        return {
            "platform": platform,
            "platforms": platforms,
            "platform_label": "三平台联合" if platform == "all" else PLATFORMS[platform],
            "scan_mode": scan_mode,
            "scan_mode_label": {"full": "全量关键词池", "quick": "快速抽样", "target": "指定岗位"}[scan_mode],
            "keyword_count": len(full_keywords) if scan_mode == "full" else len(config.get("quick_keywords") or []) if scan_mode == "quick" else 1,
            "limit": limit,
            "limit_per_platform": limit_per_platform,
            "pages": pages,
            "city": city,
            "keyword": keyword,
        }

    def build_command(self, request: dict[str, Any]) -> list[str]:
        command = [
            self.python_executable,
            str(self.project_root / "job_crawler_runner.py"),
            "run",
            "--pages",
            str(request["pages"]),
            "--pipeline-limit",
            str(request["limit_per_platform"]),
            "--collection-limit",
            str(request["limit_per_platform"]),
            "--scan-mode",
            request["scan_mode"],
            "--source-dir",
            str(self.source_dir),
            "--output-root",
            str(self.output_root),
            "--neo4j-config",
            str(self.neo4j_config),
            "--non-interactive",
            "--fresh-scan",
            "--system-import",
            "--system-publish",
        ]
        for platform in request["platforms"]:
            command.extend(("--platform", platform))
        if request["city"]:
            command.extend(("--city", request["city"]))
        if request["scan_mode"] == "target":
            command.extend(("--keyword", request["keyword"]))
        return command

    def _update(self, run_id: str, **changes: Any) -> None:
        with self._lock:
            if self._state.get("run_id") == run_id:
                self._state.update(changes)

    def _run(self, run_id: str, request: dict[str, Any]) -> None:
        radar_dir = self.output_root / "radar_runs" / run_id
        radar_dir.mkdir(parents=True, exist_ok=True)
        log_path = radar_dir / "pipeline.log"
        command = self.build_command(request)
        try:
            self._update(
                run_id,
                status="running",
                stage="crawling",
                progress=8,
                message=f"正在以{request['scan_mode_label']}巡检{request['platform_label']}，本轮最多接入 {request['limit']} 条",
            )
            with log_path.open("w", encoding="utf-8", newline="") as log:
                process = subprocess.Popen(
                    command,
                    cwd=self.project_root,
                    env=os.environ.copy(),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                with self._lock:
                    self._process = process
                while process.poll() is None:
                    self._update_from_log(run_id, log_path)
                    time.sleep(0.7)
                return_code = int(process.returncode or 0)
            self._update_from_log(run_id, log_path)
            manifest = self._latest_manifest(self._state["started_at"])
            result = self._summarize(manifest, log_path, return_code)
            succeeded = return_code == 0
            self._update(
                run_id,
                status="completed" if succeeded else "failed",
                stage="completed" if succeeded else "failed",
                progress=100,
                message=(
                    "采集、增量入图和新岗位发现已完成"
                    if succeeded
                    else "任务未完成，请查看错误摘要后重试"
                ),
                result=result,
                finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            )
        except Exception as exc:  # noqa: BLE001 - background boundary must report failures
            self._update(
                run_id,
                status="failed",
                stage="failed",
                progress=100,
                message=f"任务启动失败：{exc}",
                result={"error": str(exc), "log": str(log_path)},
                finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            )
        finally:
            with self._lock:
                self._process = None

    def _update_from_log(self, run_id: str, log_path: Path) -> None:
        try:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-16000:]
        except OSError:
            return
        stages = (
            ("7/7", "discovering", 92, "正在识别新岗位与能力变化"),
            ("6/7", "publishing", 82, "正在发布并切换活动图谱"),
            ("5/7", "normalizing", 68, "正在生成岗位与能力关系"),
            ("4/7", "mapping", 57, "正在映射受控岗位分类"),
            ("3/7", "extracting", 43, "正在分析岗位能力并回标证据"),
            ("2/7", "filtering", 31, "正在筛选信息技术岗位"),
            ("1/7", "ingesting", 20, "采集完成，正在增量写入原始审计层"),
        )
        for marker, stage, progress, message in stages:
            if marker in tail:
                self._update(run_id, stage=stage, progress=progress, message=message)
                return

    def _latest_manifest(self, started_at: str) -> dict[str, Any]:
        runs_dir = self.output_root / "runs"
        if not runs_dir.is_dir():
            return {}
        candidates = sorted(runs_dir.glob("*/manifest.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in candidates:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if str(data.get("started_at") or "") >= started_at:
                data["manifest_path"] = str(path)
                return data
        return {}

    @staticmethod
    def _summarize(manifest: dict[str, Any], log_path: Path, return_code: int) -> dict[str, Any]:
        platforms = manifest.get("platforms") or []
        rows_added = sum(int(item.get("rows_added") or 0) for item in platforms)
        integration = manifest.get("system_integration") or {}
        error = ""
        if return_code:
            try:
                lines = [line.strip() for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
                error = lines[-1][-500:] if lines else f"进程退出码 {return_code}"
            except OSError:
                error = f"进程退出码 {return_code}"
        return {
            "return_code": return_code,
            "rows_added": rows_added,
            "platforms": [
                {
                    "platform": item.get("platform"),
                    "status": item.get("status"),
                    "rows_added": item.get("rows_added", 0),
                }
                for item in platforms
            ],
            "graph_integration": integration.get("status", "unknown"),
            "published": bool(integration.get("publish_requested")),
            "error": error,
            "manifest": manifest.get("manifest_path", ""),
            "log": str(log_path),
        }
