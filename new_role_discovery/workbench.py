"""Local workbench for asynchronous Neo4j new-role discovery runs.

The workbench reads the live global Neo4j graph through the dedicated,
read-only evolution source.  Browser clients cannot select filesystem paths,
upload databases, provide Neo4j credentials, or override model settings.
"""

from __future__ import annotations

import json
import os
import re
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .config import EvolutionConfig
from .engine import EvolutionEngine
from .neo4j_source import (
    Neo4jEvolutionSource,
    Neo4jEvolutionSourceError,
    inspect_neo4j_source,
)
from .review_repository import EvolutionReviewRepository


MAX_JSON_BYTES = 64 * 1024
MAAS_API_KEY_ENV = "IFLYTEK_MAAS_API_KEY"
TASK_ID_PATTERN = re.compile(r"^task_[0-9a-f]{32}$")
DEFAULT_PAGE_PATH = (
    Path(__file__).resolve().parent / "static" / "index.html"
)
RUN_PARAMETER_NAMES = {
    "cutoff",
    "as_of",
    "baseline_days",
    "current_days",
    "role_review_limit",
    "skill_review_limit",
    "skill_change_source_family",
    "llm_mode",
}
ARTIFACT_CONTENT_TYPES = {
    "run_manifest.json": "application/json; charset=utf-8",
    "data_quality_report.json": "application/json; charset=utf-8",
    "new_role_candidates.json": "application/json; charset=utf-8",
    "role_skill_changes.json": "application/json; charset=utf-8",
    "watchlist.json": "application/json; charset=utf-8",
    "llm_role_analysis.json": "application/json; charset=utf-8",
    "llm_usage.json": "application/json; charset=utf-8",
}
RESULT_ARTIFACTS = {
    "manifest": "run_manifest.json",
    "new_role_candidates": "new_role_candidates.json",
    "role_skill_changes": "role_skill_changes.json",
    "data_quality": "data_quality_report.json",
    "llm_usage": "llm_usage.json",
}
ACTIVE_STATES = {"QUEUED", "RUNNING"}
TERMINAL_STATES = {"REVIEW_READY", "FAILED", "INTERRUPTED"}
SAFE_SOURCE_FIELDS = {
    "type",
    "label",
    "connected",
    "schema_ready",
    "active_normalization_run_id",
    "normalization_status",
    "usable_jds",
    "min_posted_at",
    "max_posted_at",
    "max_observed_epoch",
    "max_domain_classified_at",
    "max_processed_at",
    "active_ingestion_runs",
    "active_processing_runs",
    "checked_at",
    "fingerprint",
}
REVIEW_DECISION_STATES = {
    "CONFIRM_NEW_ROLE": "CONFIRMED_NEW_ROLE",
    "CONFIRM_SPECIALIZATION": "CONFIRMED_SPECIALIZATION",
    "MERGE_ALIAS": "MERGED_ALIAS",
    "WATCH": "WATCHING",
    "REJECT_NOISE": "REJECTED",
}
REVIEW_PAYLOAD_FIELDS = {
    "expected_version",
    "decision",
    "reviewer",
    "comment",
    "definition",
}
DEFINITION_FIELDS = {
    "name",
    "parent_role_id",
    "core_responsibilities",
    "required_skills",
    "bonus_skills",
    "industry_scenarios",
    "expert_supplied_fields",
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _resolved(path: Path) -> Path:
    return Path(path).expanduser().resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _atomic_json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_maas_api_key() -> str:
    """Load the server credential without logging or returning it to HTTP."""
    api_key = os.getenv(MAAS_API_KEY_ENV, "").strip()
    if api_key or os.name != "nt":
        return api_key
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Environment",
            0,
            winreg.KEY_READ,
        ) as environment_key:
            registry_value, _ = winreg.QueryValueEx(
                environment_key,
                MAAS_API_KEY_ENV,
            )
    except (ImportError, FileNotFoundError, OSError):
        return ""
    api_key = str(registry_value or "").strip()
    if api_key:
        os.environ[MAAS_API_KEY_ENV] = api_key
    return api_key


def _safe_source_metadata(info: dict[str, Any]) -> dict[str, Any]:
    """Whitelist public graph metadata and exclude all connection fields."""
    return {
        key: value
        for key, value in info.items()
        if key in SAFE_SOURCE_FIELDS
    }


def _unavailable_source() -> dict[str, Any]:
    return {
        "type": "neo4j",
        "label": "Neo4j 全局岗位图谱",
        "connected": False,
        "schema_ready": False,
        "active_ingestion_runs": 0,
        "active_processing_runs": 0,
        "checked_at": _now(),
    }


def _source_ready(source: dict[str, Any]) -> bool:
    return bool(
        source.get("connected")
        and source.get("schema_ready")
        and int(source.get("active_ingestion_runs") or 0) == 0
        and int(source.get("active_processing_runs") or 0) == 0
    )


class WorkbenchError(Exception):
    """A safe client-facing workbench error."""

    def __init__(
        self,
        status: HTTPStatus,
        code: str,
        message: str,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def check_workbench(
    neo4j_config_path: Path,
    data_root: Path,
) -> dict[str, Any]:
    """Check Neo4j and local result storage without exposing credentials."""
    config_path = _resolved(neo4j_config_path)
    data_root = _resolved(data_root)
    try:
        source = _safe_source_metadata(inspect_neo4j_source(config_path))
        source_error = ""
    except Exception:
        source = _unavailable_source()
        source_error = "无法连接或检查 Neo4j 全局岗位图谱。"

    existing_parent = data_root
    while not existing_parent.exists() and existing_parent != existing_parent.parent:
        existing_parent = existing_parent.parent
    root_is_usable = bool(
        (not data_root.exists() or data_root.is_dir())
        and os.access(existing_parent, os.W_OK)
    )
    data_root_status = {
        "path": str(data_root),
        "exists": data_root.exists(),
        "is_directory": data_root.is_dir() if data_root.exists() else False,
        "existing_parent": str(existing_parent),
        "parent_writable": os.access(existing_parent, os.W_OK),
        "usable": root_is_usable,
    }
    page_status = {
        "path": str(DEFAULT_PAGE_PATH),
        "exists": DEFAULT_PAGE_PATH.is_file(),
    }
    ready = bool(
        _source_ready(source)
        and root_is_usable
        and page_status["exists"]
    )
    return {
        "ok": ready,
        "ready": ready,
        "source": source,
        "source_error": source_error,
        "data_root": data_root_status,
        "page": page_status,
        "qwen_configured": bool(_load_maas_api_key()),
    }


class EvolutionWorkbenchService:
    """Single-source, single-worker Neo4j new-role task manager."""

    def __init__(self, neo4j_config_path: Path, data_root: Path):
        self.neo4j_config_path = _resolved(neo4j_config_path)
        self.data_root = _resolved(data_root)
        self.jobs_root = self.data_root / "role_evolution_jobs"
        self.output_root = self.data_root / "role_evolution_runs"
        self.jobs_path = self.jobs_root / "jobs.json"

        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.output_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="role-evolution",
        )
        self._closed = False
        self._tasks: dict[str, dict[str, Any]] = self._load_tasks()
        self._review_repo: EvolutionReviewRepository | None = None
        self._review_repo_error = ""

    def close(self, *, wait: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=not wait)

    def health(self) -> dict[str, Any]:
        try:
            source = _safe_source_metadata(
                inspect_neo4j_source(self.neo4j_config_path)
            )
            source_error = ""
        except Exception:
            source = _unavailable_source()
            source_error = "无法连接或检查 Neo4j 全局岗位图谱。"
        with self._lock:
            counts = {
                state: sum(
                    task.get("status") == state
                    for task in self._tasks.values()
                )
                for state in (
                    "QUEUED",
                    "RUNNING",
                    "REVIEW_READY",
                    "FAILED",
                    "INTERRUPTED",
                )
            }
        try:
            review_storage = self._review_repository().health()
        except Exception:
            review_storage = {
                "backend": "neo4j",
                "mode": "versioned_review_subgraph",
                "available": False,
                "error": "审核版本子图当前不可用。",
            }
        return {
            "status": "ok" if _source_ready(source) else "degraded",
            "source": source,
            "source_error": source_error,
            "qwen_configured": bool(_load_maas_api_key()),
            "worker_count": 1,
            "active_run": bool(
                counts.get("QUEUED", 0) or counts.get("RUNNING", 0)
            ),
            "task_counts": counts,
            "page_available": DEFAULT_PAGE_PATH.is_file(),
            "review_storage": review_storage,
        }

    def create_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._closed:
            raise WorkbenchError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "service_closed",
                "服务正在关闭。",
            )
        parameters = self._validate_run_parameters(payload)
        with self._lock:
            if any(
                task.get("status") in ACTIVE_STATES
                for task in self._tasks.values()
            ):
                raise WorkbenchError(
                    HTTPStatus.CONFLICT,
                    "run_already_active",
                    "已有岗位演化任务正在排队或运行，请等待其完成。",
                )

            self._ensure_source_ready()
            task_id = f"task_{uuid.uuid4().hex}"
            task = {
                "task_id": task_id,
                "status": "QUEUED",
                "progress": 0,
                "stage": "QUEUED",
                "message": "任务已进入单线程执行队列。",
                "created_at": _now(),
                "started_at": "",
                "completed_at": "",
                "parameters": parameters,
                "run_id": "",
                "output_dir": "",
                "summary": {},
                "warnings": [],
                "error": "",
            }
            self._tasks[task_id] = task
            self._save_tasks_locked()
            try:
                self._executor.submit(
                    self._execute_task,
                    task_id,
                    parameters,
                )
            except RuntimeError as error:
                task.update(
                    {
                        "status": "FAILED",
                        "progress": 100,
                        "stage": "FAILED",
                        "message": "任务无法进入执行队列。",
                        "completed_at": _now(),
                        "error": "任务调度失败。",
                    }
                )
                self._save_tasks_locked()
                raise WorkbenchError(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "task_submission_failed",
                    "任务暂时无法提交。",
                ) from error
            return self._public_task(task)

    submit_run = create_run

    def list_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self._public_task(task)
                for task in sorted(
                    self._tasks.values(),
                    key=lambda item: str(item.get("created_at") or ""),
                    reverse=True,
                )
            ]

    def get_run(self, task_id: str) -> dict[str, Any]:
        return self._public_task(self._task_or_error(task_id))

    def get_result(self, task_id: str) -> dict[str, Any]:
        task = self._task_or_error(task_id)
        if task.get("status") != "REVIEW_READY":
            raise WorkbenchError(
                HTTPStatus.CONFLICT,
                "result_not_ready",
                "任务尚未生成可查看的结果。",
            )
        output_dir = self._task_output_dir(task)
        result: dict[str, Any] = {"task": self._public_task(task)}
        list_results = {"new_role_candidates", "role_skill_changes"}
        for key, name in RESULT_ARTIFACTS.items():
            path = output_dir / name
            if not path.is_file():
                result[key] = [] if key in list_results else {}
                continue
            try:
                value = _read_json(path)
                if key == "new_role_candidates" and isinstance(value, list):
                    result["candidate_result_window"] = {
                        "returned": min(len(value), 50),
                        "total": len(value),
                        "full_artifact": "new_role_candidates.json",
                    }
                    value = value[:50]
                result[key] = value
            except (OSError, ValueError, json.JSONDecodeError) as error:
                raise WorkbenchError(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "artifact_invalid",
                    f"结果文件 {name} 无法读取。",
                ) from error
        candidates = result.get("new_role_candidates") or []
        candidate_ids = [
            str(candidate.get("candidate_id") or "")
            for candidate in candidates
            if str(candidate.get("candidate_id") or "")
        ]
        try:
            reviews = self._review_repository().load_reviews(candidate_ids)
            for candidate in candidates:
                candidate["human_review"] = reviews.get(
                    str(candidate.get("candidate_id") or ""),
                    {
                        "candidate_version": 0,
                        "current_state": "DISCOVERED",
                        "definition": {},
                        "review_history": [],
                    },
                )
            result["review_storage"] = {
                "backend": "neo4j",
                "available": True,
            }
        except Exception:
            result["review_storage"] = {
                "backend": "neo4j",
                "available": False,
                "message": "审核版本子图当前不可用。",
            }
        return result

    def save_candidate_review(
        self,
        task_id: str,
        candidate_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        task = self._task_or_error(task_id)
        if task.get("status") != "REVIEW_READY":
            raise WorkbenchError(
                HTTPStatus.CONFLICT,
                "result_not_ready",
                "任务尚未进入人工审核阶段。",
            )
        if not isinstance(payload, dict):
            raise WorkbenchError(
                HTTPStatus.BAD_REQUEST,
                "invalid_review",
                "审核内容必须是 JSON 对象。",
            )
        unknown = sorted(set(payload) - REVIEW_PAYLOAD_FIELDS)
        if unknown:
            raise WorkbenchError(
                HTTPStatus.BAD_REQUEST,
                "unknown_review_fields",
                f"不支持的审核字段：{', '.join(unknown)}。",
            )
        output_dir = self._task_output_dir(task)
        candidates = _read_json(output_dir / "new_role_candidates.json")
        candidate = next(
            (
                item
                for item in candidates
                if str(item.get("candidate_id") or "") == candidate_id
            ),
            None,
        )
        if candidate is None:
            raise WorkbenchError(
                HTTPStatus.NOT_FOUND,
                "candidate_not_found",
                "岗位候选不存在。",
            )
        decision = str(payload.get("decision") or "").strip().upper()
        if decision not in REVIEW_DECISION_STATES:
            raise WorkbenchError(
                HTTPStatus.BAD_REQUEST,
                "invalid_decision",
                "审核决定无效。",
            )
        try:
            expected_version = int(payload.get("expected_version", 0))
        except (TypeError, ValueError) as error:
            raise WorkbenchError(
                HTTPStatus.BAD_REQUEST,
                "invalid_version",
                "候选版本必须是非负整数。",
            ) from error
        if expected_version < 0:
            raise WorkbenchError(
                HTTPStatus.BAD_REQUEST,
                "invalid_version",
                "候选版本必须是非负整数。",
            )
        reviewer = str(payload.get("reviewer") or "本地岗位专家").strip()[:80]
        comment = str(payload.get("comment") or "").strip()
        if not comment:
            raise WorkbenchError(
                HTTPStatus.BAD_REQUEST,
                "comment_required",
                "请填写审核依据或修改说明。",
            )
        if len(comment) > 1000:
            raise WorkbenchError(
                HTTPStatus.BAD_REQUEST,
                "comment_too_long",
                "审核说明不能超过1000字。",
            )
        definition = self._validate_definition(payload.get("definition"))
        if (
            decision in {"CONFIRM_SPECIALIZATION", "MERGE_ALIAS"}
            and not definition["parent_role_id"]
        ):
            raise WorkbenchError(
                HTTPStatus.BAD_REQUEST,
                "parent_role_required",
                "岗位细分或别名合并必须填写目标既有岗位ID。",
            )
        try:
            saved = self._review_repository().save_review(
                candidate_id=candidate_id,
                run_id=str(task.get("run_id") or ""),
                expected_version=expected_version,
                decision=decision,
                state=REVIEW_DECISION_STATES[decision],
                reviewer=reviewer,
                comment=comment,
                definition=definition,
            )
        except Exception as error:
            raise WorkbenchError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "review_storage_unavailable",
                "无法把审核结果保存到 Neo4j 版本子图。",
            ) from error
        if saved is None:
            raise WorkbenchError(
                HTTPStatus.CONFLICT,
                "candidate_version_conflict",
                "候选已被其他审核修改，请刷新后重试。",
            )
        return saved

    @staticmethod
    def _validate_definition(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise WorkbenchError(
                HTTPStatus.BAD_REQUEST,
                "invalid_definition",
                "岗位定义必须是对象。",
            )
        unknown = sorted(set(value) - DEFINITION_FIELDS)
        if unknown:
            raise WorkbenchError(
                HTTPStatus.BAD_REQUEST,
                "unknown_definition_fields",
                f"不支持的岗位定义字段：{', '.join(unknown)}。",
            )
        name = str(value.get("name") or "").strip()
        if not name or len(name) > 80:
            raise WorkbenchError(
                HTTPStatus.BAD_REQUEST,
                "invalid_role_name",
                "岗位名称不能为空且不能超过80字。",
            )

        def string_list(
            field: str,
            maximum: int,
            item_maximum: int,
        ) -> list[str]:
            raw = value.get(field) or []
            if not isinstance(raw, list) or len(raw) > maximum:
                raise WorkbenchError(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_definition_list",
                    f"{field} 必须是最多{maximum}项的列表。",
                )
            output: list[str] = []
            for item in raw:
                text = str(item or "").strip()
                if not text or len(text) > item_maximum:
                    raise WorkbenchError(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_definition_item",
                        f"{field} 含空值或超长内容。",
                    )
                if text not in output:
                    output.append(text)
            return output

        expert_fields = string_list("expert_supplied_fields", 6, 40)
        if any(field not in DEFINITION_FIELDS for field in expert_fields):
            raise WorkbenchError(
                HTTPStatus.BAD_REQUEST,
                "invalid_expert_fields",
                "专家补充字段标记无效。",
            )
        return {
            "name": name,
            "parent_role_id": str(
                value.get("parent_role_id") or ""
            ).strip()[:120],
            "core_responsibilities": string_list(
                "core_responsibilities",
                12,
                500,
            ),
            "required_skills": string_list("required_skills", 20, 120),
            "bonus_skills": string_list("bonus_skills", 20, 120),
            "industry_scenarios": string_list(
                "industry_scenarios",
                12,
                200,
            ),
            "expert_supplied_fields": expert_fields,
        }

    def _review_repository(self) -> EvolutionReviewRepository:
        if self._review_repo is not None:
            return self._review_repo
        try:
            repository = EvolutionReviewRepository(self.neo4j_config_path)
            repository.ensure_schema()
        except Exception as error:
            self._review_repo_error = (
                f"{type(error).__name__}: {str(error)[:200]}"
            )
            raise
        self._review_repo = repository
        self._review_repo_error = ""
        return repository

    def get_artifact(self, task_id: str, name: str) -> tuple[bytes, str]:
        if name not in ARTIFACT_CONTENT_TYPES:
            raise WorkbenchError(
                HTTPStatus.NOT_FOUND,
                "artifact_not_found",
                "结果文件不存在或不允许下载。",
            )
        task = self._task_or_error(task_id)
        if task.get("status") not in TERMINAL_STATES:
            raise WorkbenchError(
                HTTPStatus.CONFLICT,
                "artifact_not_ready",
                "任务尚未生成可下载的结果文件。",
            )
        path = self._task_output_dir(task) / name
        if not path.is_file():
            raise WorkbenchError(
                HTTPStatus.NOT_FOUND,
                "artifact_not_found",
                "结果文件不存在。",
            )
        try:
            return path.read_bytes(), ARTIFACT_CONTENT_TYPES[name]
        except OSError as error:
            raise WorkbenchError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "artifact_unreadable",
                "结果文件无法读取。",
            ) from error

    def _ensure_source_ready(self) -> None:
        try:
            source = _safe_source_metadata(
                inspect_neo4j_source(self.neo4j_config_path)
            )
        except Exception as error:
            raise WorkbenchError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "neo4j_unavailable",
                "Neo4j 全局岗位图谱当前不可用。",
            ) from error
        if not source.get("schema_ready"):
            raise WorkbenchError(
                HTTPStatus.PRECONDITION_FAILED,
                "neo4j_schema_not_ready",
                "Neo4j 尚无可用的全局 JD 与活动归一化版本。",
            )
        if (
            int(source.get("active_ingestion_runs") or 0) > 0
            or int(source.get("active_processing_runs") or 0) > 0
        ):
            raise WorkbenchError(
                HTTPStatus.CONFLICT,
                "upstream_run_active",
                "Neo4j 正在导入或处理数据，请稍后再运行。",
            )

    def _load_tasks(self) -> dict[str, dict[str, Any]]:
        if not self.jobs_path.is_file():
            return {}
        try:
            payload = _read_json(self.jobs_path)
        except (OSError, ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, list):
            return {}
        tasks: dict[str, dict[str, Any]] = {}
        changed = False
        for item in payload:
            if not isinstance(item, dict):
                continue
            task_id = str(item.get("task_id") or "")
            if not TASK_ID_PATTERN.fullmatch(task_id):
                continue
            task = dict(item)
            # Interrupted tasks and completed tasks whose artifacts were
            # deliberately cleaned have no reusable result.  Do not keep
            # presenting those stale records in the public run list.
            if task.get("status") == "INTERRUPTED":
                changed = True
                continue
            if task.get("status") == "REVIEW_READY":
                output_value = str(task.get("output_dir") or "")
                output_path = Path(output_value).resolve() if output_value else None
                if (
                    output_path is None
                    or not _is_within(output_path, self.output_root)
                    or not output_path.is_dir()
                ):
                    changed = True
                    continue
            if task.get("status") in ACTIVE_STATES:
                task.update(
                    {
                        "status": "INTERRUPTED",
                        "stage": "INTERRUPTED",
                        "message": "服务重启前任务未完成，请重新运行。",
                        "completed_at": _now(),
                        "error": "任务因服务重启而中断。",
                    }
                )
                changed = True
            tasks[task_id] = task
        if changed:
            _atomic_json_write(self.jobs_path, list(tasks.values()))
        return tasks

    def _save_tasks_locked(self) -> None:
        _atomic_json_write(self.jobs_path, list(self._tasks.values()))

    def _task_or_error(self, task_id: str) -> dict[str, Any]:
        if not TASK_ID_PATTERN.fullmatch(str(task_id or "")):
            raise WorkbenchError(
                HTTPStatus.NOT_FOUND,
                "task_not_found",
                "运行任务不存在。",
            )
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise WorkbenchError(
                    HTTPStatus.NOT_FOUND,
                    "task_not_found",
                    "运行任务不存在。",
                )
            return dict(task)

    @staticmethod
    def _public_task(task: dict[str, Any]) -> dict[str, Any]:
        public = dict(task)
        public.pop("output_dir", None)
        return public

    def _task_output_dir(self, task: dict[str, Any]) -> Path:
        value = str(task.get("output_dir") or "")
        if not value:
            raise WorkbenchError(
                HTTPStatus.NOT_FOUND,
                "output_not_found",
                "任务没有可用的输出目录。",
            )
        path = Path(value).resolve()
        if not _is_within(path, self.output_root):
            raise WorkbenchError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "invalid_output_path",
                "任务输出路径无效。",
            )
        return path

    def _validate_run_parameters(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise WorkbenchError(
                HTTPStatus.BAD_REQUEST,
                "invalid_json",
                "运行参数必须是 JSON 对象。",
            )
        unknown = sorted(set(payload) - RUN_PARAMETER_NAMES)
        if unknown:
            raise WorkbenchError(
                HTTPStatus.BAD_REQUEST,
                "unknown_parameters",
                f"不支持的运行参数：{', '.join(unknown)}。",
            )
        cutoff = self._parse_date(payload.get("cutoff"), end_of_day=False)
        as_of = self._parse_date(payload.get("as_of"), end_of_day=True)
        if cutoff and as_of and cutoff > as_of:
            raise WorkbenchError(
                HTTPStatus.BAD_REQUEST,
                "invalid_window",
                "新旧数据分界日不能晚于数据截止日。",
            )
        baseline_days = self._bounded_integer(
            payload.get("baseline_days", 0),
            "baseline_days",
            0,
            3650,
        )
        current_days = self._bounded_integer(
            payload.get("current_days", 30),
            "current_days",
            1,
            3650,
        )
        role_limit = self._bounded_integer(
            payload.get("role_review_limit", 50),
            "role_review_limit",
            0,
            50,
        )
        skill_limit = self._bounded_integer(
            payload.get("skill_review_limit", 5),
            "skill_review_limit",
            0,
            100,
        )
        skill_source = str(payload.get("skill_change_source_family") or "").strip()
        if len(skill_source) > 80:
            raise WorkbenchError(
                HTTPStatus.BAD_REQUEST,
                "invalid_parameter",
                "skill_change_source_family 不能超过 80 个字符。",
            )
        llm_mode = str(payload.get("llm_mode") or "auto").lower()
        if llm_mode not in {"off", "auto", "required"}:
            raise WorkbenchError(
                HTTPStatus.BAD_REQUEST,
                "invalid_llm_mode",
                "llm_mode 只能是 off、auto 或 required。",
            )
        qwen_available = bool(_load_maas_api_key())
        if llm_mode == "required" and not qwen_available:
            raise WorkbenchError(
                HTTPStatus.PRECONDITION_FAILED,
                "qwen_not_configured",
                "服务器尚未配置讯飞 MaaS APIKey。",
            )
        return {
            "cutoff": cutoff.isoformat(timespec="seconds") if cutoff else None,
            "as_of": as_of.isoformat(timespec="seconds") if as_of else None,
            "baseline_days": baseline_days,
            "current_days": current_days,
            "role_review_limit": role_limit,
            "skill_review_limit": skill_limit,
            "skill_change_source_family": skill_source,
            "llm_mode": llm_mode,
            "qwen_enabled": llm_mode != "off" and qwen_available,
        }

    @staticmethod
    def _bounded_integer(
        value: Any,
        name: str,
        minimum: int,
        maximum: int,
    ) -> int:
        if isinstance(value, bool):
            raise WorkbenchError(
                HTTPStatus.BAD_REQUEST,
                "invalid_parameter",
                f"{name} 必须是整数。",
            )
        if isinstance(value, float) and not value.is_integer():
            raise WorkbenchError(
                HTTPStatus.BAD_REQUEST,
                "invalid_parameter",
                f"{name} 必须是整数。",
            )
        if isinstance(value, str) and not re.fullmatch(r"[+-]?\d+", value.strip()):
            raise WorkbenchError(
                HTTPStatus.BAD_REQUEST,
                "invalid_parameter",
                f"{name} 必须是整数。",
            )
        try:
            number = int(value)
        except (TypeError, ValueError) as error:
            raise WorkbenchError(
                HTTPStatus.BAD_REQUEST,
                "invalid_parameter",
                f"{name} 必须是整数。",
            ) from error
        if number < minimum or number > maximum:
            raise WorkbenchError(
                HTTPStatus.BAD_REQUEST,
                "invalid_parameter",
                f"{name} 必须在 {minimum} 到 {maximum} 之间。",
            )
        return number

    @staticmethod
    def _parse_date(value: Any, *, end_of_day: bool) -> datetime | None:
        text = str(value or "").strip()
        if not text or text.lower() == "auto":
            return None
        try:
            day = datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError as error:
            raise WorkbenchError(
                HTTPStatus.BAD_REQUEST,
                "invalid_date",
                "日期必须使用 YYYY-MM-DD 或 auto。",
            ) from error
        return datetime.combine(day, time.max if end_of_day else time.min)

    def _execute_task(
        self,
        task_id: str,
        parameters: dict[str, Any],
    ) -> None:
        self._update_task(
            task_id,
            status="RUNNING",
            progress=2,
            stage="STARTING",
            message="正在准备 Neo4j 只读分析。",
            started_at=_now(),
        )

        def progress_callback(
            stage: str,
            percent: int,
            message: str = "",
        ) -> None:
            self._update_task(
                task_id,
                status="RUNNING",
                progress=max(2, min(int(percent), 99)),
                stage=str(stage or "RUNNING"),
                message=str(message or "正在分析。")[:300],
            )

        try:
            cutoff = (
                datetime.fromisoformat(parameters["cutoff"])
                if parameters["cutoff"]
                else None
            )
            as_of = (
                datetime.fromisoformat(parameters["as_of"])
                if parameters["as_of"]
                else None
            )
            config = EvolutionConfig(
                database_path=None,
                output_root=self.output_root,
                source_backend="neo4j",
                neo4j_config_path=self.neo4j_config_path,
                cutoff=cutoff,
                as_of=as_of,
                baseline_days=parameters["baseline_days"],
                current_days=parameters["current_days"],
                skill_change_source_family=parameters[
                    "skill_change_source_family"
                ],
                role_review_limit=parameters["role_review_limit"],
                skill_review_limit=parameters["skill_review_limit"],
                llm_enabled=bool(parameters["qwen_enabled"]),
                llm_provider="iflytek_maas_openai",
                llm_api_key_env=MAAS_API_KEY_ENV,
                dry_run=True,
            )
            source = Neo4jEvolutionSource(
                self.neo4j_config_path,
                progress_callback=progress_callback,
            )
            result = EvolutionEngine(
                config,
                progress_callback=progress_callback,
                data_source=source,
            ).run()
            output_dir = Path(str(result["output_dir"])).resolve()
            if not _is_within(output_dir, self.output_root):
                raise RuntimeError(
                    "Evolution engine returned an invalid output path"
                )
            persistence_warning = ""
            try:
                manifest = _read_json(output_dir / "run_manifest.json")
                candidates = _read_json(
                    output_dir / "new_role_candidates.json"
                )
                self._review_repository().persist_run(
                    task_id=task_id,
                    run_id=str(result.get("run_id") or ""),
                    manifest=manifest,
                    candidates=candidates,
                )
            except Exception:
                persistence_warning = (
                    "候选已生成，但 Neo4j 审核版本子图暂时无法写入。"
                )
            task_warnings = list(result.get("warnings") or [])
            if persistence_warning:
                task_warnings.append(persistence_warning)
            self._update_task(
                task_id,
                status="REVIEW_READY",
                progress=100,
                stage="REVIEW_READY",
                message="分析完成，结论已生成。",
                completed_at=_now(),
                run_id=str(result.get("run_id") or ""),
                output_dir=str(output_dir),
                summary=dict(result.get("summary") or {}),
                warnings=task_warnings,
                error="",
            )
        except Exception as error:
            failure = {
                "at": _now(),
                "task_id": task_id,
                "error": f"{type(error).__name__}: {error}",
                "traceback": traceback.format_exc(),
            }
            _atomic_json_write(
                self.jobs_root / f"{task_id}.failure.json",
                failure,
            )
            if isinstance(error, Neo4jEvolutionSourceError):
                public_error = "Neo4j 数据源不可用或在读取期间发生变化。"
            else:
                public_error = "岗位演化分析未能完成。"
            self._update_task(
                task_id,
                status="FAILED",
                progress=100,
                stage="FAILED",
                message="运行失败，请检查数据状态或服务器日志。",
                completed_at=_now(),
                error=public_error,
            )

    def _update_task(self, task_id: str, **changes: Any) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.update(changes)
            self._save_tasks_locked()


class EvolutionWorkbenchRequestHandler(BaseHTTPRequestHandler):
    """Same-origin HTTP adapter for the Neo4j workbench."""

    service: EvolutionWorkbenchService
    server_version = "EvolutionWorkbench/2.0"

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self._security_headers()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        try:
            if path in {"/", "/index.html"}:
                self._page()
                return
            if path in {"/ability-changes", "/ability-changes.html"}:
                self._page(DEFAULT_PAGE_PATH.with_name("ability_changes.html"))
                return
            if path == "/api/v1/evolution/health":
                self._json(self.service.health())
                return
            if path == "/api/v1/evolution/datasets":
                raise WorkbenchError(
                    HTTPStatus.GONE,
                    "datasets_retired",
                    "数据集选择已停用，工作台现在直接读取 Neo4j 全局图谱。",
                )
            if path == "/api/v1/evolution/runs":
                self._json({"items": self.service.list_runs()})
                return

            prefix = "/api/v1/evolution/runs/"
            if path.startswith(prefix):
                remainder = path.removeprefix(prefix).strip("/")
                parts = remainder.split("/") if remainder else []
                if len(parts) == 1:
                    self._json(self.service.get_run(parts[0]))
                    return
                if len(parts) == 2 and parts[1] == "result":
                    self._json(self.service.get_result(parts[0]))
                    return
                if len(parts) == 3 and parts[1] == "artifacts":
                    payload, content_type = self.service.get_artifact(
                        parts[0],
                        parts[2],
                    )
                    self._bytes(payload, content_type, parts[2])
                    return
            raise WorkbenchError(
                HTTPStatus.NOT_FOUND,
                "not_found",
                "接口不存在。",
            )
        except WorkbenchError as error:
            self._workbench_error(error)
        except Exception:
            self._server_error()

    def do_POST(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/api/v1/evolution/datasets/upload":
                self.close_connection = True
                raise WorkbenchError(
                    HTTPStatus.GONE,
                    "upload_retired",
                    "数据库上传已停用，工作台现在直接读取 Neo4j 全局图谱。",
                )
            if path == "/api/v1/evolution/runs":
                length = self._content_length(maximum=MAX_JSON_BYTES)
                try:
                    payload = json.loads(
                        self.rfile.read(length).decode("utf-8") or "{}"
                    )
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise WorkbenchError(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_json",
                        "请求正文不是有效的 JSON。",
                    ) from error
                task = self.service.create_run(payload)
                self._json(task, HTTPStatus.ACCEPTED)
                return
            prefix = "/api/v1/evolution/runs/"
            if path.startswith(prefix):
                remainder = path.removeprefix(prefix).strip("/")
                parts = remainder.split("/") if remainder else []
                if (
                    len(parts) == 4
                    and parts[1] == "candidates"
                    and parts[3] == "review"
                ):
                    length = self._content_length(maximum=MAX_JSON_BYTES)
                    try:
                        payload = json.loads(
                            self.rfile.read(length).decode("utf-8") or "{}"
                        )
                    except (
                        UnicodeDecodeError,
                        json.JSONDecodeError,
                    ) as error:
                        raise WorkbenchError(
                            HTTPStatus.BAD_REQUEST,
                            "invalid_json",
                            "请求正文不是有效的 JSON。",
                        ) from error
                    saved = self.service.save_candidate_review(
                        parts[0],
                        parts[2],
                        payload,
                    )
                    self._json(saved, HTTPStatus.CREATED)
                    return
            raise WorkbenchError(
                HTTPStatus.NOT_FOUND,
                "not_found",
                "接口不存在。",
            )
        except WorkbenchError as error:
            self._workbench_error(error)
        except Exception:
            self._server_error()

    def _content_length(self, *, maximum: int) -> int:
        raw = self.headers.get("Content-Length", "")
        try:
            length = int(raw)
        except (TypeError, ValueError) as error:
            raise WorkbenchError(
                HTTPStatus.LENGTH_REQUIRED,
                "content_length_required",
                "请求必须提供有效的 Content-Length。",
            ) from error
        if length <= 0:
            raise WorkbenchError(
                HTTPStatus.LENGTH_REQUIRED,
                "content_length_required",
                "请求必须提供有效的 Content-Length。",
            )
        if length > maximum:
            raise WorkbenchError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "request_too_large",
                "请求正文超过允许大小。",
            )
        return length

    def _json(
        self,
        value: Any,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(payload)

    def _bytes(self, payload: bytes, content_type: str, filename: str) -> None:
        safe_name = Path(filename).name
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{safe_name}"',
        )
        self._security_headers()
        self.end_headers()
        self.wfile.write(payload)

    def _page(self, page_path: Path = DEFAULT_PAGE_PATH) -> None:
        if not page_path.is_file():
            raise WorkbenchError(
                HTTPStatus.NOT_FOUND,
                "page_not_found",
                "岗位演化工作台页面尚未安装。",
            )
        try:
            payload = page_path.read_bytes()
        except OSError as error:
            raise WorkbenchError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "page_unreadable",
                "岗位演化工作台页面无法读取。",
            ) from error
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(payload)

    def _workbench_error(self, error: WorkbenchError) -> None:
        self._json(
            {
                "error": error.code,
                "message": error.message,
            },
            error.status,
        )

    def _server_error(self) -> None:
        self._json(
            {
                "error": "internal_error",
                "message": "服务器处理请求时发生错误。",
            },
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )

    def _security_headers(self) -> None:
        # No Access-Control-Allow-Origin header: the workbench is same-origin.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "font-src 'self'; "
            "object-src 'none'; "
            "base-uri 'none'; "
            "frame-ancestors 'none'; "
            "form-action 'self'",
        )

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[Evolution API] {self.address_string()} - {format % args}")


def run_workbench(
    neo4j_config_path: Path,
    data_root: Path,
    host: str = "127.0.0.1",
    port: int = 8070,
) -> None:
    """Run the local, same-origin Neo4j evolution workbench."""
    service = EvolutionWorkbenchService(neo4j_config_path, data_root)
    handler = type(
        "ConfiguredEvolutionWorkbenchRequestHandler",
        (EvolutionWorkbenchRequestHandler,),
        {"service": service},
    )
    server = ThreadingHTTPServer((host, int(port)), handler)
    server.daemon_threads = True
    print(f"岗位演化工作台已启动：http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close(wait=True)


__all__ = [
    "ARTIFACT_CONTENT_TYPES",
    "EvolutionWorkbenchRequestHandler",
    "EvolutionWorkbenchService",
    "WorkbenchError",
    "check_workbench",
    "run_workbench",
]
